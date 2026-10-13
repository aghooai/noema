"""History selection without a summary model (spec-04 §9; IMPL-045).

Generation history: the most recent committed turns verbatim, selected newest-first within the budget and emitted
in chronological order. Decision context: the text of the last 2 committed turns, at most 2,000 tokens (oldest
cut first at a turn boundary, otherwise truncated from its start).

Stored turn content (`turn.input_parts`, this module's format): a list of
`{"type": "text", "text"}`, `{"type": "json", "json"}`, `{"type": "image", "file_id"}` and
`{"type": "file", "file_id"}` in part order; `None` when the turn stored metadata only.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

import orjson

from noema.core.protocols.storage import Row
from noema.core.values import JsonValue
from noema.turns.request import HistoryOptions

DECISION_CONTEXT_TURNS: Final = 2
DECISION_CONTEXT_TOKENS: Final = 2000
HISTORY_SCAN_LIMIT: Final = 64  # newest committed turns read per turn (the budget usually stops much earlier)
CHARS_PER_TOKEN: Final = 4.0


@dataclass(frozen=True, slots=True)
class MediaText:
    """The text derivative of a history media part outside the previous media-bearing turn (IMPL-045): `kind` is
    the `<untrusted_input>` kind; `cut` is the label line of a budget cut (`[doc-3: pages 1-k of n]`) or None."""

    kind: Literal["markdown", "ocr_text"]
    text: str
    cut: str | None = None


@dataclass(frozen=True, slots=True)
class HistoryTurn:
    """One committed turn as history: its stored parts, the assistant text and the token estimate of both."""

    turn_id: str
    seq: int
    parts: tuple[Mapping[str, JsonValue], ...]
    output_text: str
    tokens: int

    @property
    def media(self) -> tuple[Mapping[str, JsonValue], ...]:
        return tuple(p for p in self.parts if p.get("type") in ("image", "file"))

    def user_text(self) -> str:
        """The user text of the turn: text parts, and JSON parts serialized, joined by newlines."""
        return "\n".join(part_text(p) for p in self.parts if p.get("type") in ("text", "json"))


def part_text(part: Mapping[str, JsonValue]) -> str:
    """The text of a stored text or JSON part."""
    if part.get("type") == "json":
        return orjson.dumps(part.get("json")).decode()
    text = part.get("text")
    return text if isinstance(text, str) else ""


def default_tokens(text: str) -> int:
    """The model-independent chars/4 estimate (spec-07 §4.5)."""
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def committed_turns(rows: Sequence[Row], estimate: Callable[[str], int] = default_tokens) -> list[HistoryTurn]:
    """The usable history rows (newest first, as `TurnRepo.recent` returns them): content stored and assistant
    text present."""
    out: list[HistoryTurn] = []
    for row in rows:
        parts = row.get("input_parts")
        text = row.get("output_text")
        if not isinstance(parts, list) or not isinstance(text, str) or not text:
            continue
        stored = tuple(p for p in parts if isinstance(p, Mapping))
        turn = HistoryTurn(str(row["id"]), int(str(row["seq"])), stored, text, 0)
        tokens = estimate(turn.user_text()) + estimate(text)
        out.append(HistoryTurn(turn.turn_id, turn.seq, stored, text, tokens))
    return out


def select_history(
    turns: Sequence[HistoryTurn], options: HistoryOptions, budget_tokens: int
) -> tuple[HistoryTurn, ...]:
    """Newest-first within `min(history.max_tokens, budget_tokens)`; `none` selects nothing, `last_n` at most n
    turns. Returned in chronological order."""
    if options.mode == "none" or budget_tokens <= 0:
        return ()
    budget = budget_tokens if options.max_tokens is None else min(options.max_tokens, budget_tokens)
    limit = options.last_n if options.mode == "last_n" and options.last_n is not None else len(turns)
    chosen: list[HistoryTurn] = []
    used = 0
    for turn in turns[:limit]:
        if used + turn.tokens > budget:
            break
        chosen.append(turn)
        used += turn.tokens
    return tuple(reversed(chosen))


def decision_context(turns: Sequence[HistoryTurn]) -> tuple[tuple[str, str], ...]:
    """(user text, assistant text) of the last 2 committed turns, chronological, at most 2,000 tokens: the oldest
    turn is dropped first; a single turn still too long keeps the end of its text."""
    recent = list(turns[:DECISION_CONTEXT_TURNS])
    pairs = [(t.user_text(), t.output_text) for t in reversed(recent)]

    def size(items: Sequence[tuple[str, str]]) -> int:
        return sum(default_tokens(u) + default_tokens(a) for u, a in items)

    while len(pairs) > 1 and size(pairs) > DECISION_CONTEXT_TOKENS:
        pairs.pop(0)
    if pairs and size(pairs) > DECISION_CONTEXT_TOKENS:
        user, assistant = pairs[0]
        excess = len(user) + len(assistant) - int(DECISION_CONTEXT_TOKENS * CHARS_PER_TOKEN)
        drop_user = min(max(0, excess), len(user))
        pairs[0] = (user[drop_user:], assistant[max(0, excess - drop_user) :])
    return tuple(pairs)
