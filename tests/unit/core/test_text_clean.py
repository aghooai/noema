"""`clean_text` (regex form, C2b perf smoke) equals its spec definition on every code point: remove general
category Cc except tab, newline and carriage return (spec-06 §5.2)."""

from __future__ import annotations

import unicodedata

import pytest

from noema.core.text import clean_text, esc

pytestmark = [pytest.mark.unit]


def _reference(s: str) -> str:
    return "".join(ch for ch in s if ch in "\t\n\r" or unicodedata.category(ch) != "Cc")


def test_every_code_point_is_kept_or_removed_as_the_definition_says() -> None:
    every = "".join(map(chr, range(0x110000)))  # lone surrogates included (a str may hold them)
    assert clean_text(every) == _reference(every)


def test_only_the_65_cc_code_points_minus_tab_newline_cr_are_removed() -> None:
    removed = [c for c in range(0x110000) if clean_text(chr(c)) == ""]
    assert removed == [c for c in range(0x110000) if unicodedata.category(chr(c)) == "Cc" and chr(c) not in "\t\n\r"]
    assert len(removed) == 62


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    [
        ("a\x00b\x1fc\x7fd\x80e\x9ff", "abcdef"),
        ("keep\ttab\nnewline\rcr", "keep\ttab\nnewline\rcr"),
        ("\u00abts:1\u00bbtext\u200b\u00ad", "\u00abts:1\u00bbtext\u200b\u00ad"),  # Cf and others stay
        ("", ""),
    ],
)
def test_examples(raw: str, cleaned: str) -> None:
    assert clean_text(raw) == cleaned


def test_esc_still_cleans_before_escaping_reserved_tags() -> None:
    assert esc("<\x00question>") == "&lt;question>"
