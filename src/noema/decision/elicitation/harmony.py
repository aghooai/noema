"""Harmony final-channel handling (gpt-oss style output): text and token level (spec-06 §5.6-§5.7)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from noema.core.protocols.adapters import TokenLogprob

HARMONY_FINAL: Final = "<|channel|>final<|message|>"
_END_MARKERS: Final = ("<|end|>", "<|return|>", "<|call|>")


def strip_harmony(text: str) -> str:
    """The text after the last final-channel marker, without a trailing end marker, stripped."""
    i = text.rfind(HARMONY_FINAL)
    if i >= 0:
        text = text[i + len(HARMONY_FINAL) :]
    for end in _END_MARKERS:
        if text.endswith(end):
            text = text[: -len(end)]
    return text.strip()


def final_start(tokens: Sequence[TokenLogprob]) -> int:
    """The index after the last `final`, `<|message|>` token pair (0 when there is none)."""
    start = 0
    for i in range(len(tokens) - 1):
        if tokens[i].token == "final" and tokens[i + 1].token == "<|message|>":  # noqa: S105 (marker tokens)
            start = i + 2
    return start
