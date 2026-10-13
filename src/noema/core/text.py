"""Prompt text helpers shared by decision and turns/prompt (spec-02 §1; exact code spec-06 §5.2, spec-09 §5;
IMPL-029a, IMPL-086)."""

from __future__ import annotations

import re
from typing import Final

RESERVED_TAGS: Final = ("untrusted_input", "app_question", "decision_context", "question", "answer")

_DATAMARK: Final = "\u02c6"  # IMPL-086: datamarked text uses it for whitespace
# Whitespace, Unicode format characters (Cf, BMP) and the datamark that may separate "<" from a reserved tag name
# (spec-06 §5.2). The name ends at anything but an ASCII word character: `\b` would not end it before `ˆ` (category
# Lm), so `</untrusted_inputˆ>` (a whitespace form once datamarked) would pass unescaped (Wave E2, finding 6).
_GAP: Final = r"[\s\u00ad\u061c\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff\u02c6]*"
_TAG: Final = re.compile(
    r"<(" + _GAP + r"/?" + _GAP + r")(" + "|".join(RESERVED_TAGS) + r")(?![0-9A-Za-z_])", re.IGNORECASE
)
_WS: Final = re.compile(r"\s+")


# General category Cc is exactly U+0000-U+001F and U+007F-U+009F (an immutable Unicode property); tab, newline and
# carriage return stay. One regex pass instead of a per-character category lookup (30x faster on prompt-sized text;
# C2b perf smoke, reports/perf/c2b-perf-smoke.md); tests/unit/core/test_text_clean.py checks every code point.
_CC: Final = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def clean_text(s: str) -> str:
    """Remove C0/C1 control characters except tab, newline and carriage return."""
    return _CC.sub("", s)


def escape_reserved_tags(s: str) -> str:
    """Rewrite `<` + optional gap + optional `/` + optional gap + a reserved tag name (case-insensitive, not
    followed by an ASCII word character) to `&lt;` + the rest, so untrusted text can neither open nor close a prompt
    block."""
    return _TAG.sub(lambda m: "&lt;" + m.group(1) + m.group(2), s)


def esc(s: str) -> str:
    """`escape_reserved_tags(clean_text(s))`: applied to every untrusted text before it enters a prompt."""
    return escape_reserved_tags(clean_text(s))


def datamark(s: str) -> str:
    """Replace every whitespace run with exactly one U+02C6 (no surrounding spaces); spotlighting (IMPL-086). A
    literal U+02C6 of the untrusted text is removed first, so every datamark in the result is one whitespace run."""
    return _WS.sub(_DATAMARK, s.replace(_DATAMARK, ""))
