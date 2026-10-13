"""The `decision-conditioning@1` block (spec-04 §5.2, spec-08 §3.2 and §14.9; ADR-004; IMPL-013, IMPL-014,
IMPL-029a, IMPL-082).

This module is the canonical rendering: only option values, 5 % bins, status words and fixed guidance reach the
model. Values are JSON-quoted with `<`, `>` and `&` JSON-escaped, so no value can close or forge the block.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Final, Literal

from noema.core.protocols.generation import Conditioning
from noema.core.text import clean_text
from noema.core.values import JsonValue

if TYPE_CHECKING:
    from noema.core.protocols.decision import AnswerSummary

TEMPLATE_ID: Final = "decision-conditioning@1"
BlockMode = Literal["cover_top2_and_clarify", "clarify_first"]
BLOCK_MODES: Final[frozenset[str]] = frozenset({"cover_top2_and_clarify", "clarify_first"})
GUIDANCE: Final[Mapping[str, str]] = {
    "cover_top2_and_clarify": "The request is ambiguous between the top two options listed above. "
    "Address both briefly, then ask one clarifying question.",
    "clarify_first": "Ask one clarifying question before answering.",
}
_SUMMARY_TAIL: Final[Mapping[str, str]] = {
    "cover_top2_and_clarify": "cover both + clarify",
    "clarify_first": "clarify first",
}
_REQUEST_CUT: Final = 64
_CLOSE_MARGIN: Final = 0.15
_UNSCORED: Final = frozenset({"unavailable", "refusal"})


def qv(value: object, request_supplied: bool) -> str:
    """One quoted data value: booleans as `true`/`false`; otherwise `clean_text`, cut to 64 characters for
    request-supplied values, JSON-quoted, with `&`, `<`, `>` as JSON escapes."""
    if isinstance(value, bool):
        return "true" if value else "false"
    s = clean_text(str(value))
    if request_supplied:
        s = s[:_REQUEST_CUT]
    return json.dumps(s, ensure_ascii=False).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


def pct(p: float) -> int:
    """The nearest 5 % bin, ties up, computed on the emitted 4-decimal value."""
    return 5 * int((Decimal(f"{p:.4f}") * 20).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _close(a: AnswerSummary) -> bool:
    return a.margin is not None and a.margin < _CLOSE_MARGIN and len(a.top) > 1


def line(a: AnswerSummary, request_supplied: bool) -> str:
    """One answer line: `name: "v1" (≈P1%)[ or "v2" (≈P2%)] — calibrated|uncalibrated, confidence <band>`."""
    v1, p1 = a.top[0]
    s = f"{a.name}: {qv(v1, request_supplied)} (≈{pct(p1)}%)"
    if _close(a):
        v2, p2 = a.top[1]
        s += f" or {qv(v2, request_supplied)} (≈{pct(p2)}%)"
    return s + f" — {'calibrated' if a.calibrated else 'uncalibrated'}, confidence {a.band}"


def _scored(answers: Mapping[str, AnswerSummary], names: Sequence[str]) -> list[AnswerSummary]:
    out = []
    for n in names:
        a = answers.get(n)
        if a is not None and a.type not in _UNSCORED and a.top:
            out.append(a)
    return out


def render(
    mode: BlockMode,
    answers: Mapping[str, AnswerSummary],
    commit_critical: Sequence[str],
    request_names: Collection[str],
) -> str:
    """The block (no trailing newline): every scored commit-critical answer, in answer order, then the guidance."""
    lines = [line(a, a.name in request_names) for a in _scored(answers, commit_critical)]
    return "\n".join(
        ['<decision_context origin="platform" v="1">', *lines, f"guidance: {GUIDANCE[mode]}", "</decision_context>"]
    )


def signature(block: str) -> str:
    """`"sha256:" + sha256("decision-conditioning@1\\n" + block)` (IMPL-014, ADR-004)."""
    return "sha256:" + hashlib.sha256((TEMPLATE_ID + "\n" + block).encode("utf-8")).hexdigest()


def _plain(value: object, request_supplied: bool) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    s = clean_text(str(value))
    return s[:_REQUEST_CUT] if request_supplied else s


def summary(
    mode: BlockMode,
    answers: Mapping[str, AnswerSummary],
    commit_critical: Sequence[str],
    request_names: Collection[str],
) -> str:
    """The display summary (not signed): `"{name} {v1}≈{P1}%[ vs {v2}≈{P2}%], {band} confidence"` per rendered
    answer, joined by "; ", then "; cover both + clarify" or "; clarify first"."""
    parts = []
    for a in _scored(answers, commit_critical):
        rs = a.name in request_names
        v1, p1 = a.top[0]
        s = f"{a.name} {_plain(v1, rs)}≈{pct(p1)}%"
        if _close(a):
            v2, p2 = a.top[1]
            s += f" vs {_plain(v2, rs)}≈{pct(p2)}%"
        parts.append(f"{s}, {a.band} confidence")
    return "; ".join([*parts, _SUMMARY_TAIL[mode]])


def build(
    mode: BlockMode,
    answers: Mapping[str, AnswerSummary],
    commit_critical: Sequence[str],
    request_names: Collection[str],
) -> Conditioning:
    """The full `Conditioning` (wire fields plus the prompt block) for one answer mode."""
    block = render(mode, answers, commit_critical, request_names)
    return Conditioning(
        answer_mode=mode,
        signature=signature(block),
        summary=summary(mode, answers, commit_critical, request_names),
        block=block,
    )


def wire(conditioning: Conditioning | None) -> dict[str, JsonValue] | None:
    """`RoutingDecision.conditioning`: `{answer_mode, signature, summary}`, or None without a block."""
    if conditioning is None:
        return None
    return {
        "answer_mode": conditioning.answer_mode,
        "signature": conditioning.signature,
        "summary": conditioning.summary,
    }
