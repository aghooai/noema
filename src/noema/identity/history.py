"""Session history messages (`SessionTurn`, spec-08 §9, IMPL-040) from stored turn rows.

Each committed turn yields its user message, then its assistant message. Stored content is the orchestrator's format
(`noema.turns.context.history`, requests/orchestrator.md item 7): `turn.input_parts` is a list of
`{"type": "text", "text"}`, `{"type": "json", "json"}`, `{"type": "image" | "file", "file_id"}` in part order (NULL
under `metadata_only`). The user text is the text parts and the JSON parts (compact `orjson` serialization) joined
by newlines, in part order; media parts have no history text. The assistant text is `output_text`.

Redactions: a `redacted` turn stores the orchestrator's marker `[REDACTED:<kind>]` in place of each match. Its spans
are the markers found in the rendered text (the orchestrator's own rule, `turns.persist._spans`), not the offsets in
`turn.redactions`: those of JSON parts do not survive the jsonb round trip, which reorders object keys. Other turns
use the stored `turn.redactions` (`{"user": [...], "assistant": [...]}`, code points, `end` exclusive; out-of-range,
overlapping or malformed spans are dropped). On the wire every span reads "[redacted]" (the contract's
`SessionTurn.text`) with recomputed offsets, its reason kept. A `metadata_only` turn stores no content: both messages
read "[redacted]" with one span covering it (reason `metadata_only`).

Paging (`history_page`): `limit` counts messages, like every other list. A page may end between a turn's user and
assistant messages; the cursor then names the repository cursor before that turn and the turn's `seq`, and the next
page starts with its assistant message (the turn is read whole again if it was deleted meanwhile).
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Final

import orjson

from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.storage import Row, TurnRepo
from noema.core.values import JsonValue

REDACTED: Final = "[redacted]"
MAX_LIMIT: Final = 200  # the contract's `Limit` maximum
STORED_MARKER: Final = re.compile(r"\[REDACTED:([a-z0-9_]+)\]")  # the orchestrator's marker (turns.persist._MARKER)
_WITHHELD: Final[list[JsonValue]] = [{"start": 0, "end": len(REDACTED), "reason": "metadata_only"}]


def part_text(part: Mapping[str, object]) -> str:
    """The history text of one stored part: a text part's text, a JSON part serialized, else "" (media)."""
    kind = part.get("type")
    if kind == "json":
        return orjson.dumps(part.get("json")).decode()
    text = part.get("text") if kind == "text" else None
    return text if isinstance(text, str) else ""


def user_text(input_parts: object) -> str:
    """The user text of a stored `input_parts` list: text and JSON parts joined by newlines, in part order."""
    if not isinstance(input_parts, Sequence) or isinstance(input_parts, str | bytes):
        return ""
    return "\n".join(part_text(p) for p in input_parts if isinstance(p, Mapping) and p.get("type") in ("text", "json"))


def _stored(text: str, value: object) -> list[tuple[int, int, str]]:
    """The well-formed stored spans within `text`."""
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    valid: list[tuple[int, int, str]] = []
    for span in value:
        if not isinstance(span, Mapping):
            continue
        start, end, reason = span.get("start"), span.get("end"), span.get("reason")
        if (
            isinstance(start, int)
            and isinstance(end, int)
            and not isinstance(start, bool)
            and not isinstance(end, bool)
            and isinstance(reason, str)
            and 0 <= start <= end <= len(text)
        ):
            valid.append((start, end, reason))
    return valid


def redact_spans(text: str, value: object, *, markers: bool = False) -> tuple[str, list[JsonValue]]:
    """`text` with every span replaced by "[redacted]", and the spans over the result. The spans are the stored
    ones (`value`), or with `markers` the orchestrator's markers found in `text`."""
    valid = (
        [(m.start(), m.end(), m.group(1)) for m in STORED_MARKER.finditer(text)] if markers else _stored(text, value)
    )
    if not valid:
        return text, []
    valid.sort()
    pieces: list[str] = []
    spans: list[JsonValue] = []
    cursor = size = 0
    for start, end, reason in valid:
        if start < cursor:
            continue  # overlaps the previous span
        pieces.append(text[cursor:start])
        size += start - cursor
        pieces.append(REDACTED)
        spans.append({"start": size, "end": size + len(REDACTED), "reason": reason})
        size += len(REDACTED)
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces), spans


def _epoch(value: object) -> int:
    return int(value.timestamp()) if isinstance(value, datetime) else 0


def turn_messages(turn: Row) -> list[JsonValue]:
    """The two `SessionTurn` objects of one turn row (user, then assistant)."""
    turn_id, seq = str(turn["id"]), turn.get("seq")
    seq_value = seq if isinstance(seq, int) else 0
    created, completed = _epoch(turn.get("created_at")), _epoch(turn.get("completed_at"))
    if turn.get("content_policy") == "metadata_only":
        texts = {"user": (REDACTED, list(_WITHHELD)), "assistant": (REDACTED, list(_WITHHELD))}
    else:
        redactions = turn.get("redactions")
        spans = redactions if isinstance(redactions, Mapping) else {}
        output = turn.get("output_text")
        markers = turn.get("content_policy") == "redacted"
        texts = {
            "user": redact_spans(user_text(turn.get("input_parts")), spans.get("user"), markers=markers),
            "assistant": redact_spans(
                output if isinstance(output, str) else "", spans.get("assistant"), markers=markers
            ),
        }
    return [
        {
            "turn_id": turn_id,
            "seq": seq_value,
            "role": "user",
            "text": texts["user"][0],
            "created_at": created,
            "redactions": texts["user"][1],
        },
        {
            "turn_id": turn_id,
            "seq": seq_value,
            "role": "assistant",
            "text": texts["assistant"][0],
            "created_at": completed,
            "redactions": texts["assistant"][1],
        },
    ]


def encode_cursor(turns_after: str | None, split_seq: int) -> str:
    """A history cursor: the turn repository's cursor and the `seq` of a half-delivered turn (0 = none)."""
    return base64.urlsafe_b64encode(orjson.dumps([turns_after, split_seq])).decode().rstrip("=")


def decode_cursor(after: str | None) -> tuple[str | None, int]:
    """The parts of a history cursor; NoemaError(schema_violation, param="after") when malformed."""
    if after is None:
        return None, 0
    try:
        value = orjson.loads(base64.urlsafe_b64decode(after + "=" * (-len(after) % 4)))
    except (ValueError, binascii.Error):  # orjson.JSONDecodeError is a ValueError
        value = None
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not (value[0] is None or isinstance(value[0], str))
        or isinstance(value[1], bool)
        or not isinstance(value[1], int)
        or value[1] < 0
    ):
        raise NoemaError(ErrorCode.SCHEMA_VIOLATION, "The pagination cursor is malformed.", param="after")
    return value[0], value[1]


async def history_page(turns: TurnRepo, session_id: str, *, after: str | None, limit: int) -> dict[str, JsonValue]:
    """One `SessionTurnList` page of at most `limit` messages, oldest first (see the module docstring)."""
    if isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
        raise NoemaError(ErrorCode.SCHEMA_VIOLATION, "limit must be between 1 and 200.", param="limit")
    cursor, split_seq = decode_cursor(after)
    data: list[JsonValue] = []
    remaining = limit
    if split_seq:
        head = await turns.list_for_session(session_id, after=cursor, limit=1)
        if head.data and head.data[0].get("seq") == split_seq:
            data.append(turn_messages(head.data[0])[1])
            remaining -= 1
            if not head.has_more:
                return _page(data, None)
            cursor = head.next_cursor
    whole, half = divmod(remaining, 2)
    if whole:
        page = await turns.list_for_session(session_id, after=cursor, limit=whole)
        for turn in page.data:
            data.extend(turn_messages(turn))
        if not page.has_more:
            return _page(data, None)
        cursor = page.next_cursor
    if half:
        tail = await turns.list_for_session(session_id, after=cursor, limit=1)
        if not tail.data:
            return _page(data, None)
        seq = tail.data[0].get("seq")
        data.append(turn_messages(tail.data[0])[0])
        return _page(data, encode_cursor(cursor, seq if isinstance(seq, int) and seq > 0 else 0))
    return _page(data, encode_cursor(cursor, 0))


def _page(data: list[JsonValue], next_cursor: str | None) -> dict[str, JsonValue]:
    return {"data": data, "has_more": next_cursor is not None, "next_cursor": next_cursor}
