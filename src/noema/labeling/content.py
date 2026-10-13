"""What a labeling job shows the labelers (IMPL-071, IMPL-043, IMPL-086; spec-09 §5).

`resolve()` loads the content of a `ContentRef` under `tenant_uow(tenant)`: the decision row's stored `input` (a
string, a JSON-state object, or a message list `[{role, content: [part, ...]}]` with `input_text`/`text`,
`input_json` and `input_image`/`input_file` (`file_id`) parts); for a turn decision, the turn's own `input_parts`
plus the history of up to `HISTORY_TURNS` earlier committed turns of its session (user parts and assistant text, at
most `HISTORY_MAX_CHARS` characters, oldest dropped first), falling back to the decision input when the turn is gone.
A decision whose row stores no input (`metadata_only`) is not labeled. Both stored part formats are read: the
decision row's (`input_text`, `input_json`, `input_image`, `input_file`; decision/repo.py) and the turn row's
(`text`, `json`, `image`, `file`; turns/context/history.py). Files are resolved with `FileService.get`;
`render()` turns one labeler's `InputView` of the media into prompt blocks: engine role labels, `esc` on every
untrusted text, the `[label: view]` line before each media part and `datamark(esc(text))` on document and OCR text.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

import orjson

from noema.core.errors import NoemaError
from noema.core.principal import Principal
from noema.core.protocols.decision import Msg
from noema.core.protocols.media import ContentPart, FileService, ViewPart
from noema.core.protocols.storage import Row, UnitOfWork
from noema.core.text import datamark, esc
from noema.core.values import ContentBlock, JsonValue, TextBlock

HISTORY_TURNS: Final = 10
HISTORY_MAX_CHARS: Final = 48_000
EXCERPT_MAX_CHARS: Final = 2_000
_TEXT_TYPES: Final = frozenset({"input_text", "text", "output_text"})
_JSON_TYPES: Final = frozenset({"input_json", "json"})
_FILE_TYPES: Final = frozenset({"input_image", "input_file", "image", "file"})


@dataclass(frozen=True, slots=True)
class LabelInput:
    """The conversation the labelers see (media parts carry their `FileRef`) and the raw excerpt text of the
    labeled input (redacted and truncated by the caller)."""

    messages: tuple[Msg, ...]
    excerpt_text: str


def canonical_json(value: object) -> str:
    """Sorted-key compact JSON (spec-06 §5.3)."""
    return orjson.dumps(value, option=orjson.OPT_SORT_KEYS).decode()


@dataclass
class _Builder:
    files: FileService
    principal: Principal
    index: int = 0
    images: int = 0
    docs: int = 0

    async def parts(self, raw: object) -> tuple[ContentPart, ...]:
        if isinstance(raw, str):
            return (self.text_part(raw),)
        if not isinstance(raw, list):
            return ()
        out: list[ContentPart] = []
        for part in raw:
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind in _TEXT_TYPES and isinstance(part.get("text"), str):
                out.append(self.text_part(str(part["text"])))
            elif kind in _JSON_TYPES:
                out.append(self.json_part(part.get("json")))
            elif kind in _FILE_TYPES and isinstance(part.get("file_id"), str):
                media = await self._file(str(part["file_id"]))
                if media is not None:
                    out.append(media)
        return tuple(out)

    def _next(self) -> int:
        self.index += 1
        return self.index - 1

    def text_part(self, text: str) -> ContentPart:
        return ContentPart(self._next(), "text", None, text=text)

    def json_part(self, value: object) -> ContentPart:
        return ContentPart(self._next(), "json", None, json=_as_json(value))

    async def _file(self, file_id: str) -> ContentPart | None:
        try:
            ref = await self.files.get(self.principal, file_id)
        except NoemaError:
            return None  # deleted or never ready: the labelers see the rest of the input
        media_type = ref.media_type or ref.declared_type
        if media_type.startswith("image/"):
            self.images += 1
            return ContentPart(self._next(), "image", f"image-{self.images}", file=ref)
        self.docs += 1
        return ContentPart(self._next(), "file", f"doc-{self.docs}", file=ref)


def _as_json(value: object) -> JsonValue:
    return orjson.loads(orjson.dumps(value))  # type: ignore[no-any-return]


async def _messages_of(raw: object, b: _Builder) -> list[Msg]:
    if isinstance(raw, list) and all(isinstance(m, dict) and "role" in m for m in raw):
        out: list[Msg] = []
        for m in raw:
            role: Literal["user", "assistant"] = "assistant" if m.get("role") == "assistant" else "user"
            parts = await b.parts(m.get("content"))
            if parts:
                out.append(Msg(role, parts))
        return out
    if isinstance(raw, str):
        return [Msg("user", (b.text_part(raw),))]
    if isinstance(raw, dict):
        return [Msg("user", (b.json_part(raw),))]
    return []


def _excerpt(messages: Sequence[Msg]) -> str:
    last_user = next((m for m in reversed(messages) if m.role == "user"), None)
    if last_user is None:
        return ""
    texts = [
        p.text if p.kind == "text" else canonical_json(p.json) for p in last_user.parts if p.kind in ("text", "json")
    ]
    return "\n".join(t for t in texts if t)


async def _history(uow: UnitOfWork, turn: Row, b: _Builder) -> list[Msg]:
    session_id, seq = turn.get("session_id"), turn.get("seq")
    if not isinstance(session_id, str) or not isinstance(seq, int) or seq <= 1:
        return []
    previous = await uow.turns.recent(session_id, max_seq=seq - 1, limit=HISTORY_TURNS)
    out: list[Msg] = []
    budget = HISTORY_MAX_CHARS
    for row in previous:  # seq DESC: newest first, so the budget drops the oldest
        user = await b.parts(row.get("input_parts"))
        answer = row.get("output_text")
        pair: list[Msg] = [Msg("user", user)] if user else []
        if isinstance(answer, str) and answer:
            pair.append(Msg("assistant", (b.text_part(answer),)))
        size = sum(len(p.text or "") for m in pair for p in m.parts)
        if size > budget:
            break
        budget -= size
        out[:0] = pair
    return out


async def resolve(
    uow: UnitOfWork, decision: Row, turn_id: str | None, files: FileService, principal: Principal
) -> LabelInput | None:
    """The labelers' content for one decision row (module docstring); None when the row stores no input."""
    stored = decision.get("input")
    if stored is None:
        return None
    b = _Builder(files, principal)
    if turn_id is not None:
        turn = await uow.turns.get(turn_id)
        if turn is not None and turn.get("input_parts") is not None:
            history = await _history(uow, turn, b)
            current = await b.parts(turn.get("input_parts"))
            if current:
                messages = (*history, Msg("user", current))
                return LabelInput(messages, _excerpt(messages))
    messages = tuple(await _messages_of(stored, b))
    if not messages:
        return None
    return LabelInput(messages, _excerpt(messages))


def media_parts(content: LabelInput) -> tuple[ContentPart, ...]:
    """Every image and file part of the conversation (unique indexes), for one InputViewResolver call."""
    return tuple(p for m in content.messages for p in m.parts if p.kind in ("image", "file"))


def render(content: LabelInput, views: Mapping[int, ViewPart]) -> tuple[ContentBlock, ...]:
    """The `<untrusted_input>` body blocks for one labeler (module docstring); adjacent text is merged."""
    blocks: list[ContentBlock] = []
    text: list[str] = []

    def flush() -> None:
        if text:
            blocks.append(TextBlock("".join(text)))
            text.clear()

    multi = len(content.messages) > 1
    for msg in content.messages:
        if multi:
            text.append(f"[{msg.role}]\n")
        for part in msg.parts:
            if part.kind == "text":
                text.append(esc(part.text or "") + "\n")
            elif part.kind == "json":
                text.append(esc(canonical_json(part.json)) + "\n")
            else:
                view = views.get(part.index)
                kind = view.kind if view is not None else "metadata_only"
                note = f"; {view.note}" if view is not None and view.note else ""
                text.append(f"[{part.label}: {kind}{note}]\n")
                for block in view.blocks if view is not None else ():
                    if isinstance(block, TextBlock):
                        text.append(datamark(esc(block.text)) + "\n")
                    else:
                        flush()
                        blocks.append(block)
    flush()
    return tuple(blocks)
