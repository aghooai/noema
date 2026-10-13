"""The JSON examples of the API user guide (docs/noema/07-api-user-guide.md), for the docs-drift check.

Extracted: ```json fences, the `-d '<json>'` bodies of ```bash curl commands, the keyword arguments of the
`client.decisions.create(...)` call in the openai ```python fence, and inline code spans that are JSON objects or
`"key": value` members. The guide abbreviates with `…`: a bare `…` (an elided member, element or object body) is
removed and marks the example `abbreviated`; a string containing `…` is a placeholder (`"dec_…"`).
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ELLIPSIS = "…"
_SENTINEL_KEY, _SENTINEL_ITEM = "__elided__", "__ELIDED__"
_FENCE = re.compile(r"^```(\w*)\n(.*?)^```", re.S | re.M)
_HEADING = re.compile(r"^(#{2,4}) (.+)$", re.M)
_CURL = re.compile(r"curl\b.*?\$AGHOO(/\S+).*?-d '(.*?)'", re.S)
_INLINE = re.compile(r"`([^`\n]+)`")


@dataclass(frozen=True)
class Example:
    section: str  # the heading above it, e.g. "4.5 Jev-shaped output"
    source: str  # json | curl | python | inline
    line: int
    text: str
    value: Any = None
    abbreviated: bool = False
    path: str | None = None  # curl: the request path


def normalize(text: str) -> tuple[Any, bool]:
    """Parse abbreviated JSON: each bare `…` up to the next `,`, `]` or `}` of its level becomes a sentinel, removed
    after parsing. Returns (value, abbreviated)."""
    out, stack, i, in_str, abbreviated = [], [], 0, False, False
    while i < len(text):
        ch = text[i]
        if in_str:
            out.append(ch)
            in_str = not (ch == '"' and text[i - 1] != "\\")
        elif ch == '"':
            in_str = True
            out.append(ch)
        elif ch in "{[":
            stack.append(ch)
            out.append(ch)
        elif ch in "}]":
            stack.pop()
            out.append(ch)
        elif ch == ELLIPSIS:
            abbreviated = True
            depth = 0
            while i + 1 < len(text) and (depth or text[i + 1] not in ",]}"):
                depth += {"{": 1, "[": 1, "}": -1, "]": -1}.get(text[i + 1], 0)
                i += 1
            out.append(f'"{_SENTINEL_KEY}": null' if stack and stack[-1] == "{" else f'"{_SENTINEL_ITEM}"')
        else:
            out.append(ch)
        i += 1
    value = _strip(json.loads("".join(out)))
    return value, abbreviated or ELLIPSIS in text


def _strip(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip(v) for k, v in node.items() if k != _SENTINEL_KEY}
    if isinstance(node, list):
        return [_strip(v) for v in node if v != _SENTINEL_ITEM]
    return node


def _sections(text: str) -> list[tuple[int, str]]:
    return [(m.start(), m.group(2).strip()) for m in _HEADING.finditer(text)]


def _section_at(sections: list[tuple[int, str]], pos: int) -> str:
    return next((title for start, title in reversed(sections) if start <= pos), "")


def _openai_kwargs(code: str) -> dict[str, Any] | None:
    for node in ast.walk(ast.parse(code)):
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "client.decisions.create":
            return {kw.arg: ast.literal_eval(kw.value) for kw in node.keywords if kw.arg}
    return None


def extract(guide: Path) -> Iterator[Example]:
    text = guide.read_text()
    sections = _sections(text)
    for fence in _FENCE.finditer(text):
        lang, body, pos = fence.group(1), fence.group(2), fence.start()
        line, section = text.count("\n", 0, pos) + 1, _section_at(sections, pos)
        if lang == "json":
            value, abbreviated = normalize(body)
            yield Example(section, "json", line, body, value, abbreviated)
        elif lang == "bash":
            for curl in _CURL.finditer(body):
                value, abbreviated = normalize(curl.group(2))
                yield Example(section, "curl", line, curl.group(2), value, abbreviated, path=curl.group(1))
        elif lang == "python" and "from openai import" in body:
            kwargs = _openai_kwargs(body)
            if kwargs is not None:
                yield Example(section, "python", line, body, kwargs, False)
    prose = _FENCE.sub(lambda m: " " * len(m.group(0)), text)
    for span in _INLINE.finditer(prose):
        snippet = span.group(1).strip()
        if not snippet.startswith(("{", '"')) or ":" not in snippet:
            continue
        line, section = text.count("\n", 0, span.start()) + 1, _section_at(sections, span.start())
        try:
            value, abbreviated = normalize(snippet if snippet.startswith("{") else "{" + snippet + "}")
        except (json.JSONDecodeError, IndexError):
            yield Example(section, "inline", line, snippet)  # value None: not JSON (pseudo-syntax)
            continue
        yield Example(section, "inline", line, snippet, value, abbreviated)
