"""Decision prompt rendering: the `app_question` JSON, the untrusted-input wrapping and escaping (spec-06 §5.1-§5.3;
IMPL-029a, IMPL-050, IMPL-086).

The system role is static platform text; the user message is `[B_q, B_open, *view blocks, B_close]`. Decision owns
every prompt byte: the resolver returns content blocks only, and the label lines, the escaping (`esc`) and the
datamarking of media text are rendered here. Decision prompts are always `standard` spotlighting.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Final

from noema.core.protocols.decision import DecisionInput, Msg, OptionSpec, VerificationInput
from noema.core.protocols.media import ContentPart, InputView, ViewPart
from noema.core.text import clean_text, datamark, esc
from noema.core.values import ContentBlock, TextBlock

AQ_OPEN: Final = "<app_question>"
AQ_CLOSE: Final = "</app_question>\n"
B_OPEN: Final = TextBlock("<untrusted_input>\n")
_CLOSE: Final = "\n</untrusted_input>\n"


def app_question(
    instructions: str, letters_to_option: Sequence[OptionSpec], criteria: tuple[str | None, str | None] | None
) -> str:
    """The `<app_question>` JSON: letter j shows `letters_to_option[j]`; `<`, `>` and `&` become JSON escapes, so
    no application string can close the block."""
    obj: dict[str, object] = {
        "instructions": clean_text(instructions),
        "options": [
            {
                "letter": chr(65 + j),
                "value": clean_text(o.key),
                "description": None if o.description is None else clean_text(o.description),
            }
            for j, o in enumerate(letters_to_option)
        ],
    }
    if criteria and any(criteria):  # predicate only; absent sides are omitted
        obj["criteria"] = {k: clean_text(v) for k, v in (("A", criteria[0]), ("B", criteria[1])) if v is not None}
    text = json.dumps(obj, ensure_ascii=False, separators=(", ", ": "))
    return text.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


def question_block(aq: str) -> TextBlock:
    """`B_q`: the cacheable question block."""
    return TextBlock(AQ_OPEN + aq + AQ_CLOSE)


def close_block(tail: str) -> TextBlock:
    """`B_close`: closes the untrusted input and adds the template's tail line."""
    return TextBlock(_CLOSE + tail)


def user_blocks(aq: str, view_blocks: Sequence[ContentBlock], tail: str) -> tuple[ContentBlock, ...]:
    """The member's user message: `[B_q, B_open, *view blocks, B_close]`."""
    return (question_block(aq), B_OPEN, *view_blocks, close_block(tail))


def canonical_json(value: object) -> str:
    """JSON state and `input_json` parts as rendered in prompts (sorted keys, compact, ensure_ascii=False)."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=_plain)


def verification_blocks(value: VerificationInput) -> tuple[ContentBlock, ...]:
    """`verify-dist@1`: one text block with `<question>` and the `<answer id>` blocks (escaped, never datamarked)."""
    text = "<question>" + esc(value.question) + "</question>\n"
    text += "".join('<answer id="' + aid + '">' + esc(atext) + "</answer>\n" for aid, atext in value.answers)
    return (TextBlock(text),)


def view_blocks(value: DecisionInput, view: InputView | None) -> tuple[ContentBlock, ...]:
    """The view blocks of one member (spec-06 §5.3). `view` holds the resolver's media parts (None: text only)."""
    if isinstance(value, VerificationInput):
        return verification_blocks(value)
    if isinstance(value, str):
        return (TextBlock(esc(value)),)
    if isinstance(value, tuple):
        return _message_blocks(value, view)
    return (TextBlock(esc(canonical_json(value))),)


def _message_blocks(messages: Sequence[Msg], view: InputView | None) -> tuple[ContentBlock, ...]:
    by_index: Mapping[int, ViewPart] = {p.part_index: p for p in view.parts} if view is not None else {}
    labelled = len(messages) > 1
    out: list[ContentBlock] = []
    for msg in messages:
        if labelled:
            out.append(TextBlock(f"[{msg.role}]\n"))  # an engine label, not escaped
        for part in msg.parts:
            out.extend(_part_blocks(part, by_index.get(part.index)))
    return tuple(out)


def _part_blocks(part: ContentPart, view_part: ViewPart | None) -> list[ContentBlock]:
    if part.kind == "text":
        return [TextBlock(esc(part.text or "") + "\n")]
    if part.kind == "json":
        return [TextBlock(esc(canonical_json(part.json)) + "\n")]
    label = part.label or f"part-{part.index + 1}"
    if view_part is None:
        return [TextBlock(f"[{label}: metadata_only]\n")]
    head = f"[{view_part.label or label}: {view_part.kind}"
    if view_part.truncated and view_part.note:
        head += f"; {view_part.note}"
    out: list[ContentBlock] = [TextBlock(head + "]\n")]
    out.extend(TextBlock(datamark(esc(b.text))) if isinstance(b, TextBlock) else b for b in view_part.blocks)
    return out


def _plain(obj: object) -> object:
    if isinstance(obj, Mapping):
        return dict(obj)
    if isinstance(obj, tuple | frozenset):
        return list(obj)
    raise TypeError(f"{type(obj).__name__} is not JSON serializable")
