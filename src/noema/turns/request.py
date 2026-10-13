"""The createTurn body as plain values (openapi `TurnCreateParams`).

The ingress gate has validated the body against the schema and the semantic rules (spec-04 §1 step 5), so this is a
cheap structural read with no Pydantic. `parts` flattens every input item's content in order (a turn's user input
is one user message, so the conversation keeps alternating roles).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal, cast

from noema.core.errors import ErrorCode, NoemaError
from noema.core.values import Effort, JsonValue
from noema.turns.spec import Pins

PartKind = Literal["text", "json", "image", "file"]
Strategy = Literal["holdback", "gate"]
_DATA_URL: Final = re.compile(r"^data:(image/[a-z0-9.+-]+);base64,", re.IGNORECASE)
# Media parts (file_id and inline images) per turn: step 6 looks up every file before admission, so their number is
# bounded (Wave E; the same bound as createDecision's)
MAX_MEDIA_PARTS: Final = 64


@dataclass(frozen=True, slots=True)
class RequestPart:
    """One input part: `index` is the flat position; `item`/`position` locate it in the body for `param` paths."""

    index: int
    item: int
    position: int
    kind: PartKind
    text: str | None = None
    json: JsonValue = None
    file_id: str | None = None
    data_b64: str | None = None
    media_type: str | None = None

    @property
    def param(self) -> str:
        return f"input[{self.item}].content[{self.position}]"


@dataclass(frozen=True, slots=True)
class HistoryOptions:
    """`history` (IMPL-045): auto (by budget), none, or last_n turns within the budget."""

    mode: Literal["auto", "none", "last_n"] = "auto"
    max_tokens: int | None = None
    last_n: int | None = None


@dataclass(frozen=True, slots=True)
class DecisionOverrides:
    """`decisions.*` (IMPL-069); `include` is accepted and ignored (turn decisions always include members)."""

    profile: str | None = None
    questions: tuple[Mapping[str, JsonValue], ...] = ()
    blocking: tuple[str, ...] = ()
    deadline_ms: int | None = None
    min_quorum: int | None = None


@dataclass(frozen=True, slots=True)
class TurnRequest:
    """A createTurn request (see the module docstring). `strategy` is Phase-1 resolved (auto -> holdback)."""

    parts: tuple[RequestPart, ...]
    session_id: str | None = None
    store: bool = True
    expected_seq: int | None = None
    stream: bool = True
    wait_for_files: bool = False
    allow_partial_files: bool = False
    retry_of: str | None = None
    instructions: str | None = None
    decisions: DecisionOverrides = field(default_factory=DecisionOverrides)
    pins: Pins = field(default_factory=Pins)
    strategy: Strategy = "holdback"
    strategy_requested: str | None = None
    history: HistoryOptions = field(default_factory=HistoryOptions)

    @property
    def file_parts(self) -> tuple[RequestPart, ...]:
        return tuple(p for p in self.parts if p.file_id is not None)


def _part(index: int, item: int, position: int, raw: Mapping[str, JsonValue]) -> RequestPart:
    kind = raw.get("type")
    if kind == "input_text":
        return RequestPart(index, item, position, "text", text=str(raw.get("text", "")))
    if kind == "input_json":
        return RequestPart(index, item, position, "json", json=raw.get("json"))
    if kind == "input_file":
        return RequestPart(index, item, position, "file", file_id=str(raw["file_id"]))
    if kind == "input_image" and isinstance(raw.get("file_id"), str):
        return RequestPart(index, item, position, "image", file_id=str(raw["file_id"]))
    url = str(raw.get("image_url", ""))
    match = _DATA_URL.match(url)
    if match is None:  # the gate rejects these first; kept as a guard for direct callers
        raise NoemaError(
            ErrorCode.SCHEMA_VIOLATION, "Unsupported input part.", param=f"input[{item}].content[{position}]"
        )
    return RequestPart(index, item, position, "image", data_b64=url[match.end() :], media_type=match.group(1).lower())


def _opt_int(value: JsonValue) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _opt_str(value: JsonValue) -> str | None:
    return value if isinstance(value, str) else None


def _mapping(value: JsonValue) -> Mapping[str, JsonValue]:
    return value if isinstance(value, Mapping) else {}


def parse_turn_request(body: JsonValue) -> TurnRequest:
    """Read a schema-valid createTurn body (no checks but the `MAX_MEDIA_PARTS` cap, 400: the out-of-phase values
    are step-8 errors, IMPL-036)."""
    b = _mapping(body)
    items = b.get("input")
    parts: list[RequestPart] = []
    for i, item in enumerate(items if isinstance(items, list) else []):
        content = _mapping(item).get("content")
        for j, raw in enumerate(content if isinstance(content, list) else []):
            parts.append(_part(len(parts), i, j, _mapping(raw)))
    if sum(p.kind in ("image", "file") for p in parts) > MAX_MEDIA_PARTS:
        raise NoemaError(
            ErrorCode.SCHEMA_VIOLATION,
            f"At most {MAX_MEDIA_PARTS} media parts per request.",
            param="input",
            details={"max_media_parts": MAX_MEDIA_PARTS},
        )
    strategy = b.get("strategy")
    gen = _mapping(b.get("generation"))
    profile = _opt_str(gen.get("profile"))
    pins = Pins(
        generation_profile=None if profile in (None, "auto") else profile,
        effort=cast("Effort | None", _opt_str(gen.get("effort"))),
        max_output_tokens=_opt_int(gen.get("max_output_tokens")),
        conditioning=_opt_str(b.get("conditioning")),
        verify=verify if isinstance(verify := b.get("verify"), bool) else None,
    )
    dec = _mapping(b.get("decisions"))
    questions = dec.get("questions")
    blocking = dec.get("blocking")
    decisions = DecisionOverrides(
        profile=_opt_str(dec.get("profile")),
        questions=tuple(_mapping(q) for q in questions) if isinstance(questions, list) else (),
        blocking=tuple(str(n) for n in blocking) if isinstance(blocking, list) else (),
        deadline_ms=_opt_int(dec.get("deadline_ms")),
        min_quorum=_opt_int(dec.get("min_quorum")),
    )
    hist = _mapping(b.get("history"))
    mode = hist.get("mode")
    history = HistoryOptions(
        mode=cast("Literal['auto', 'none', 'last_n']", mode) if mode in ("auto", "none", "last_n") else "auto",
        max_tokens=_opt_int(hist.get("max_tokens")),
        last_n=_opt_int(hist.get("last_n")),
    )
    return TurnRequest(
        parts=tuple(parts),
        session_id=_opt_str(b.get("session_id")),
        store=b.get("store") is not False,
        expected_seq=_opt_int(b.get("expected_seq")),
        stream=b.get("stream") is not False,
        wait_for_files=b.get("wait_for_files") is True,
        allow_partial_files=b.get("allow_partial_files") is True,
        retry_of=_opt_str(b.get("retry_of")),
        instructions=_opt_str(b.get("instructions")),
        decisions=decisions,
        pins=pins,
        strategy="gate" if strategy == "gate" else "holdback",
        strategy_requested=_opt_str(strategy),
        history=history,
    )


def final_message_text(parts: Sequence[RequestPart]) -> str:
    """The text parts of the final user message (input item), joined by newlines: the verifier question
    (IMPL-024)."""
    last = max((p.item for p in parts), default=0)
    return "\n".join(p.text for p in parts if p.item == last and p.kind == "text" and p.text)
