"""Generation prompt assembly (spec-04 §9, spec-09 §5.3; IMPL-045, IMPL-086, ADR-004).

Order: `system = (PREAMBLE_V1, instructions?)`; the history messages (user/assistant, chronological; the previous
media-bearing turn's media negotiated per model, older media as `<untrusted_input>` text derivatives, IMPL-045);
the current user message (escaped text, media blocks from the `MediaNegotiator`, whose `<untrusted_input>` bodies
are always datamarked); the conditioning block last (ADR-004). `strict` spotlighting additionally datamarks the
user and history text of attempts that start after its trigger became final. The system role, `instructions` and
the conditioning block are never datamarked. Filenames never appear: media are `image-N` / `doc-N`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

import orjson

from noema.core.principal import Principal
from noema.core.protocols.generation import AssembledPrompt, Conditioning
from noema.core.protocols.media import ContentPart, MediaNegotiator, MediaPlan
from noema.core.text import datamark, esc
from noema.core.values import ContentBlock, Degradation, Message, TextBlock
from noema.turns.concurrent import gather
from noema.turns.context.history import HistoryTurn, MediaText
from noema.turns.prompt.preamble import PREAMBLE_V1

if TYPE_CHECKING:
    from noema.core.protocols.config import ConfigSnapshot


def media_kind(part: ContentPart) -> str:
    """`image` or `document` (label lines)."""
    if part.kind == "image":
        return "image"
    media_type = (part.file.media_type or part.file.declared_type) if part.file is not None else ""
    return "image" if media_type.startswith("image/") else "document"


@dataclass(frozen=True, slots=True)
class PromptInputs:
    """Everything the builder needs, resolved by preflight. `history_labels` maps history file ids to the labels
    of this turn's numbering (current parts first, then the previous media-bearing turn, then older turns);
    `previous` are the resolved media parts of the previous media-bearing turn (`previous_turn_id`);
    `history_texts` the text derivatives of the older history media (by file id, within the allowance T)."""

    snapshot: ConfigSnapshot
    principal: Principal
    instructions: str | None
    history: tuple[HistoryTurn, ...]
    current: tuple[ContentPart, ...]
    plan: MediaPlan
    previous: tuple[ContentPart, ...] = ()
    previous_turn_id: str | None = None
    history_labels: Mapping[str, str] = field(default_factory=dict)
    history_texts: Mapping[str, MediaText] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _Native:
    """A slot for the previous media-bearing turn's negotiated blocks (per alias), with its fallback line."""

    key: int
    fallback: TextBlock


type _Item = ContentBlock | _Native
_PREVIOUS: Final = 1_000_000  # media keys: current part index, or _PREVIOUS + the previous turn's part index


def untrusted(label: str, kind: str, text: str) -> TextBlock:
    """A text derivative as `<untrusted_input>` with a datamarked body (spec-09 §5.3; the tags are not marked)."""
    return TextBlock(f'<untrusted_input source="{label}" kind="{kind}">\n{datamark(esc(text))}\n</untrusted_input>')


class TurnPrompt:
    """The turn's prompt state shared by its runs; `strict` is raised by the orchestrator (IMPL-086). The escaped
    (and, for strict, datamarked) text of the history and the current message is built once per mode, and the
    media once per alias; a build only assembles them."""

    def __init__(self, inputs: PromptInputs, negotiator: MediaNegotiator) -> None:
        self.inputs = inputs
        self._negotiator = negotiator
        self.strict = False
        self.builds = 0
        self._system = (PREAMBLE_V1,) if not inputs.instructions else (PREAMBLE_V1, inputs.instructions)
        self._media: dict[str, tuple[dict[int, tuple[ContentBlock, ...]], tuple[Degradation, ...]]] = {}
        self._items: dict[bool, tuple[tuple[tuple[_Item, ...], TextBlock], ...]] = {}
        self._current: dict[bool, tuple[_Item, ...]] = {}
        self._older: dict[str, tuple[ContentBlock, ...]] = {}

    def for_run(self, conditioning: Conditioning | None) -> RunPrompt:
        """The `PromptBuilder` of one profile run (A1 has no conditioning, IMPL-014)."""
        return RunPrompt(self, conditioning)

    def _text(self, s: str, strict: bool) -> TextBlock:
        escaped = esc(s)
        return TextBlock(datamark(escaped) if strict else escaped)

    async def _negotiated(self, alias: str) -> tuple[dict[int, tuple[ContentBlock, ...]], tuple[Degradation, ...]]:
        """Every media part's blocks for `alias` (negotiated concurrently, once per alias)."""
        cached = self._media.get(alias)
        if cached is not None:
            return cached
        inputs = self.inputs
        model = inputs.snapshot.model(alias)
        jobs = [
            (origin * _PREVIOUS + part.index, part)
            for origin, parts in ((0, inputs.current), (1, inputs.previous))
            for part in parts
            if part.kind in ("image", "file")
        ]
        out: dict[int, tuple[tuple[ContentBlock, ...], tuple[Degradation, ...]]] = {}

        def negotiate(key: int, part: ContentPart) -> Callable[[], Awaitable[None]]:
            async def call() -> None:
                out[key] = await self._negotiator.negotiate(
                    alias, model, (part,), inputs.plan, principal=inputs.principal
                )

            return call

        await gather([negotiate(key, part) for key, part in jobs])
        by_part = {key: out[key][0] for key, _ in jobs}
        result = (by_part, tuple(d for key, _ in jobs for d in out[key][1]))
        self._media[alias] = result
        return result

    def _older_blocks(self, file_id: str, label: str) -> tuple[ContentBlock, ...]:
        """An older media part: its text derivative (IMPL-045), else `[label: not available]` (spec-07 §4.2)."""
        cached = self._older.get(file_id)
        if cached is None:
            text = self.inputs.history_texts.get(file_id)
            if text is None:
                cached = (TextBlock(f"[{label}: not available]"),)
            else:
                block = untrusted(label, text.kind, text.text)
                cached = (block,) if text.cut is None else (block, TextBlock(text.cut))
            self._older[file_id] = cached
        return cached

    def _history_user(self, turn: HistoryTurn, strict: bool) -> tuple[_Item, ...]:
        items: list[_Item] = []
        text = turn.user_text()
        if text:
            items.append(self._text(text, strict))
        native = turn.turn_id == self.inputs.previous_turn_id
        for part in turn.media:
            file_id = str(part.get("file_id", ""))
            label = self.inputs.history_labels.get(file_id, "media")
            if not native:
                items.extend(self._older_blocks(file_id, label))
                continue
            prev = next((p for p in self.inputs.previous if p.file is not None and p.file.file_id == file_id), None)
            fallback = TextBlock(f"[{label}: not available]")
            items.append(_Native(_PREVIOUS + prev.index, fallback) if prev is not None else fallback)
        return tuple(items)

    def _history(self, strict: bool) -> tuple[tuple[tuple[_Item, ...], TextBlock], ...]:
        cached = self._items.get(strict)
        if cached is None:
            cached = self._items[strict] = tuple(
                (self._history_user(turn, strict), self._text(turn.output_text, strict)) for turn in self.inputs.history
            )
        return cached

    def _current_items(self, strict: bool) -> tuple[_Item, ...]:
        cached = self._current.get(strict)
        if cached is None:
            items: list[_Item] = []
            for part in self.inputs.current:
                if part.kind == "text":
                    items.append(self._text(part.text or "", strict))
                elif part.kind == "json":
                    items.append(self._text(orjson.dumps(part.json).decode(), strict))
                else:
                    items.append(_Native(part.index, TextBlock(f"[{part.label or 'media'}: not available]")))
            cached = self._current[strict] = tuple(items)
        return cached

    @staticmethod
    def _blocks(items: Sequence[_Item], media: Mapping[int, Sequence[ContentBlock]]) -> list[ContentBlock]:
        out: list[ContentBlock] = []
        for item in items:
            if isinstance(item, _Native):
                out.extend(media.get(item.key) or (item.fallback,))
            else:
                out.append(item)
        return out

    async def build(self, alias: str, conditioning: Conditioning | None) -> AssembledPrompt:
        """One attempt's prompt for `alias` (see the module docstring)."""
        self.builds += 1
        strict = self.strict
        media, degradations = await self._negotiated(alias)
        messages: list[Message] = []
        for user, assistant in self._history(strict):
            messages.append(Message("user", tuple(self._blocks(user, media))))
            messages.append(Message("assistant", (assistant,)))
        current = self._blocks(self._current_items(strict), media)
        if conditioning is not None:
            current.append(TextBlock(conditioning.block))
        messages.append(Message("user", tuple(current)))
        return AssembledPrompt(self._system, tuple(messages), conditioning, degradations)


class RunPrompt:
    """Implements `PromptBuilder` for one run: the shared turn prompt plus the run's conditioning."""

    def __init__(self, turn: TurnPrompt, conditioning: Conditioning | None) -> None:
        self._turn = turn
        self.conditioning = conditioning

    async def build(self, alias: str) -> AssembledPrompt:
        return await self._turn.build(alias, self.conditioning)
