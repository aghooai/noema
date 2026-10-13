"""The fixed Markdown renderer of `markdown.sanitize` and `pdf.markdown` (spec-09 §6.5).

The input is parsed with markdown-it-py `MarkdownIt("js-default")` (HTML, linkify and typographer off) and the
token tree is re-serialized: ATX headings, paragraphs, `-`/`N.` lists, `>` quotes, `---`, pipe tables and code in
backtick fences longer than any backtick run inside. Text (also alt and link text and table cells) is emitted with
`\\ ` `` ` `` `* _ [ ] < > & ! | ~` backslash-escaped, plus a leading `# + - =` or `N.`/`N)` at a line start, so raw
HTML survives only as text. Images become `\\[image: {alt}\\]`; links become `[text](<href>)` only for http, https
and mailto; reference definitions vanish; autolinks become their text.

`sanitize` is idempotent: it re-renders until a fixed point (at most `MAX_PASSES`), and falls back to escaped plain
paragraphs (a fixed point by construction) if none is reached. Each pass drops the previous token tree before the
next parse, so the peak is one tree (about 250 MiB per MiB of input); `manual_gc` (the one-shot `markdown.sanitize`
process) also pauses the automatic cyclic GC while a tree is built and collects once per pass instead, about 30%
less CPU (5 MiB: ~30 s CPU, ~1.3 GiB RSS; media-pipeline docs, C1 review).
"""

from __future__ import annotations

import gc
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from markdown_it.tree import SyntaxTreeNode

SAFE_SCHEMES: Final = frozenset({"http", "https", "mailto"})
MAX_PASSES: Final = 4
_ESCAPED: Final = frozenset("\\`*_[]<>&!|~")
_LINE_START: Final = re.compile(r"[#+\-=]|\d{1,9}[.)]")
_CONTROL: Final = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_INFO: Final = re.compile(r"[A-Za-z0-9_+.#-]{1,32}")
_HREF_ENCODE: Final[dict[int, str]] = {ord(c): f"%{ord(c):02X}" for c in "<> \t\n\r\x0b\x0c\\|"}
_ALIGN: Final = {"left": ":---", "center": ":---:", "right": "---:"}


def _parser() -> Callable[[str], SyntaxTreeNode]:
    from markdown_it import MarkdownIt
    from markdown_it.tree import SyntaxTreeNode

    md = MarkdownIt("js-default")
    return lambda text: SyntaxTreeNode(md.parse(text))


def escape_text(text: str, *, line_start: bool, hashes: bool = False) -> str:
    """Backslash-escape the Markdown punctuation of `text` (and a block-starting prefix at a line start); `hashes`
    also escapes every `#` (heading text, whose trailing `#`s would otherwise close the heading)."""
    text = _CONTROL.sub(" ", text)
    out = "".join(f"\\{ch}" if ch in _ESCAPED or (hashes and ch == "#") else ch for ch in text)
    if line_start and (m := _LINE_START.match(out)):
        cut = m.end() - 1
        out = f"{out[:cut]}\\{out[cut:]}"
    return out


def safe_href(href: str) -> str | None:
    """The href for `[text](<href>)`, or None when its scheme is not http, https or mailto."""
    scheme, sep, _ = href.partition(":")
    if not sep or scheme.lower() not in SAFE_SCHEMES:
        return None
    return href.translate(_HREF_ENCODE).replace("&", "\\&")


def _code_span(content: str) -> str:
    runs = [len(r) for r in re.findall(r"`+", content)]
    fence = "`" * (max(runs, default=0) + 1)
    if content.strip(" ") and (content[0] in " `" or content[-1] in " `"):
        content = f" {content} "
    return f"{fence}{content}{fence}"


def plain_text(node: SyntaxTreeNode) -> str:
    """The text content of an inline subtree (for image alt text)."""
    if node.type in ("text", "code_inline", "html_inline"):
        return node.content
    if node.type in ("softbreak", "hardbreak"):
        return " "
    return "".join(plain_text(c) for c in node.children)


class _Inline:
    """Renders one inline subtree; tracks whether the next text starts a line."""

    def __init__(self, *, heading: bool = False) -> None:
        self.parts: list[str] = []
        self.line_start = True
        self.heading = heading

    def emit(self, text: str) -> None:
        if text:
            self.parts.append(text)
            self.line_start = False

    def text(self, content: str) -> None:
        self.emit(escape_text(content, line_start=self.line_start, hashes=self.heading))

    def node(self, node: SyntaxTreeNode) -> None:
        kind = node.type
        if kind in ("text", "html_inline", "text_special"):
            self.text(node.content)
        elif kind == "softbreak":
            self.parts.append("\n")
            self.line_start = True
        elif kind == "hardbreak":
            self.parts.append("\\\n")
            self.line_start = True
        elif kind == "code_inline":
            self.emit(_code_span(node.content.replace("\n", " ")))
        elif kind in ("em", "strong", "s"):
            markup = node.markup or {"em": "*", "strong": "**", "s": "~~"}[kind]
            self.emit(markup)
            self.children(node)
            self.emit(markup)
        elif kind == "image":
            self.text(f"[image: {plain_text(node)}]")
        elif kind == "link":
            href = safe_href(str(node.attrs.get("href", "")))
            if href is None or node.markup == "autolink":
                self.children(node)
            else:
                self.emit("[")
                self.children(node)
                self.emit(f"](<{href}>)")
        else:
            self.children(node)

    def children(self, node: SyntaxTreeNode) -> None:
        for child in node.children:
            self.node(child)


def render_inline(node: SyntaxTreeNode, *, heading: bool = False) -> str:
    """The Markdown of an `inline` node."""
    r = _Inline(heading=heading)
    r.children(node)
    return "".join(r.parts)


def _fence(content: str, info: str) -> list[str]:
    runs = [len(r) for r in re.findall(r"`+", content)]
    fence = "`" * max(3, max(runs, default=0) + 1)
    lang = m.group(0) if (m := _INFO.match(info.strip())) else ""
    body = content if content.endswith("\n") or not content else content + "\n"
    return [f"{fence}{lang}", *body.split("\n")[:-1], fence]


def _prefixed(lines: list[str], first: str, rest: str, *, blank: str = "") -> list[str]:
    out = []
    for i, line in enumerate(lines):
        prefix = first if i == 0 else rest
        out.append((prefix + line) if line else (prefix.rstrip() if i == 0 else blank))
    return out or [first.rstrip()]


def _list(node: SyntaxTreeNode) -> list[str]:
    ordered = node.type == "ordered_list"
    start = int(str(node.attrs.get("start", 1))) if ordered else 1
    items = node.children
    tight = all(p.hidden for item in items for p in item.children if p.type == "paragraph")
    lines: list[str] = []
    for i, item in enumerate(items):
        number = start + i if start + i <= 999_999_999 else start
        marker = f"{number}{item.markup or '.'} " if ordered else f"{item.markup or '-'} "
        if i and not tight:
            lines.append("")
        lines.extend(_prefixed(render_blocks(item.children, tight=tight), marker, " " * len(marker)))
    return lines


def _cell(node: SyntaxTreeNode) -> str:
    return render_inline(node.children[0]) if node.children else ""


def _table(node: SyntaxTreeNode) -> list[str]:
    rows = [tr for section in node.children for tr in section.children]
    if not rows:
        return []
    header = rows[0].children
    width = len(header)
    aligns = []
    for th in header:
        style = str(th.attrs.get("style", ""))
        aligns.append(_ALIGN.get(style.removeprefix("text-align:"), "---"))
    lines = ["| " + " | ".join(_cell(c) for c in header) + " |", "| " + " | ".join(aligns) + " |"]
    for tr in rows[1:]:
        cells = [_cell(c) for c in tr.children[:width]]
        cells += [""] * (width - len(cells))
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def render_block(node: SyntaxTreeNode) -> list[str]:
    """The lines of one block node."""
    kind = node.type
    if kind == "paragraph":
        return render_inline(node.children[0]).split("\n") if node.children else []
    if kind == "heading":
        text = render_inline(node.children[0], heading=True) if node.children else ""
        return [("#" * int(node.tag[1:]) + " " + text).rstrip()]
    if kind == "hr":
        return ["---"]
    if kind in ("fence", "code_block"):
        return _fence(node.content, node.info if kind == "fence" else "")
    if kind == "blockquote":
        return _prefixed(render_blocks(node.children), "> ", "> ", blank=">")
    if kind in ("bullet_list", "ordered_list"):
        return _list(node)
    if kind == "table":
        return _table(node)
    if kind == "html_block":
        return [escape_text(line.strip(), line_start=True) for line in node.content.split("\n") if line.strip()]
    return render_blocks(node.children)


def render_blocks(nodes: list[SyntaxTreeNode], *, tight: bool = False) -> list[str]:
    """Blocks separated by one blank line (none in tight list items)."""
    lines: list[str] = []
    for node in nodes:
        block = render_block(node)
        if not block:
            continue
        if lines and not tight:
            lines.append("")
        lines.extend(block)
    return lines


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL.sub(" ", text)


def _render(tree: SyntaxTreeNode) -> str:
    lines = [line.rstrip(" ") for line in render_blocks(tree.children)]
    return "\n".join(lines) + "\n" if lines else ""


def _plain(tree: SyntaxTreeNode) -> str:
    """Fallback: every block's text as escaped plain paragraphs."""
    paragraphs: list[str] = []
    for node in tree.walk():
        if node.type == "inline":
            text = plain_text(node)
        elif node.type in ("fence", "code_block"):
            text = node.content
        else:
            continue
        lines = [escape_text(line.strip(), line_start=True) for line in text.split("\n") if line.strip()]
        if lines:
            paragraphs.append("\n".join(lines))
    return "\n\n".join(paragraphs) + "\n" if paragraphs else ""


def _collect() -> None:
    gc.collect()


def _no_collect() -> None:
    return None


@contextmanager
def _gc_paused(paused: bool) -> Iterator[Callable[[], None]]:
    """With `paused`: no automatic cyclic GC inside the block; the yielded `collect()` runs one collection."""
    was_enabled = gc.isenabled()
    if paused:
        gc.disable()
    try:
        yield _collect if paused else _no_collect
    finally:
        if paused and was_enabled:
            gc.enable()


def sanitize(text: str, *, manual_gc: bool = False) -> str:
    """The sanitized Markdown of `text` (see the module docstring); idempotent."""
    parse = _parser()
    with _gc_paused(manual_gc) as collect:
        current = _render(parse(_clean(text)))  # the tree is garbage once rendered
        for _ in range(MAX_PASSES):
            collect()
            following = _render(parse(current))
            if following == current:
                return current
            current = following
        collect()
        return _plain(parse(_clean(text)))
