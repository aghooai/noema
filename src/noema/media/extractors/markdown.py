"""`markdown.sanitize` (spec-09 §6.5, ADR-002 §5): strict UTF-8 (checked by `detect`), empty after whitespace
stripping → `empty_content`, else the fixed renderer → `markdown.md` (one segment)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Final

from noema.core.values import JsonValue
from noema.media.extractors.common import Outputs, Rejection, version_of
from noema.media.extractors.md_render import sanitize

EXTRACTOR: Final = f"{version_of('markdown-it-py')}+md_render@1"


def run(input_path: Path, out: Outputs, params: Mapping[str, JsonValue]) -> None:
    """Sanitize one Markdown upload."""
    try:
        text = input_path.read_bytes().decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise Rejection("corrupt") from None
    if not text.strip():
        raise Rejection("empty_content")
    data = sanitize(text, manual_gc=True).replace("\f", " ").encode("utf-8")  # one-shot process: see md_render
    out.add("markdown.md", data, kind="markdown", media_type="text/markdown")
