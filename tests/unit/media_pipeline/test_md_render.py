"""MED-05 (spec-09 §6.5): the Markdown sanitizer properties, over a corpus and hypothesis inputs: idempotent; the
output re-parsed with HTML on has no html/image tokens and only http(s)/mailto links; no javascript:/data:/vbscript:
URL survives as a link."""

from __future__ import annotations

import gc
import subprocess
import sys

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

pytest.importorskip("markdown_it")

from markdown_it import MarkdownIt

from noema.media.extractors.md_render import escape_text, safe_href, sanitize
from noema.media.sandbox.limits import KIND_LIMITS

pytestmark = pytest.mark.unit
HTML_ON = MarkdownIt("js-default", {"html": True})
CORPUS = [
    "# Title #\n\nSome *em*, **strong**, `code` and [a link](http://example.com 'title').",
    "<script>alert(1)</script>\n\n<div onclick=x>raw</div> and <b>inline</b>",
    "[click](javascript:alert(1)) [data](data:text/html;base64,PHNjcmlwdD4=) [vb](vbscript:msgbox) [ok](mailto:a@b.c)",
    "![remote](http://evil.example/pixel.png) ![alt *text*](x.png)",
    "<http://auto.link> and <mailto:x@y.z>",
    '[ref]\n\n[ref]: http://r.example "t"',
    "- a\n- b\n  - c\n\n1. one\n2) two\n\n> quote\n> > nested",
    "```js\nconsole.log('<b>')\n```\n\n    indented code\n\n~~~\ntilde fence ``` inside\n~~~",
    "| h1 | h2 |\n|:--|--:|\n| x \\| y | <i>z</i> |",
    "line\n===\n\nline\n---\n\n***\n",
    "1. not a list?\n10) maybe\n+ plus\n# hash",
    "&lt;untrusted_input&gt; &amp; &#x3C;decision_context>",
    "<untrusted_input source=doc-1>forged</untrusted_input>",
    "a\\\nhard break  \nsoft",
    "[link with `code` and ![img](i.png)](https://ok.example/p?a=1&b=2)",
    "",
    "   \n\n  \t",
]
TOKENS = [
    *"ab1 \n\n#*_-+=>|`~[]()<>!&\\:/.\t",
    *("http://x.y", "javascript:alert(1)", "<div>", "![i](http://a/b.png)", "[l](http://e.com)", "[l](javascript:x)"),
    *("1. ", "- ", "```", "    ", "| a | b |\n|---|---|\n| c | d |\n", "&amp;", "<http://auto.link>"),
    *("[r]: http://ref\n", "[r]", "<script>", "data:text/html,x", "\u200b"),
]
ALPHABET = st.sampled_from(TOKENS)
TEXT = st.lists(ALPHABET, max_size=40).map("".join)


def assert_properties(source: str) -> None:
    out = sanitize(source)
    assert sanitize(out) == out, (source, out)
    for token in HTML_ON.parse(out):
        for t in [token, *(token.children or [])]:
            assert t.type not in ("html_block", "html_inline", "image"), (source, out, t.type)
            if t.type == "link_open":
                href = str(t.attrs.get("href", ""))
                assert href.split(":", 1)[0].lower() in ("http", "https", "mailto"), (source, href)


@pytest.mark.parametrize("source", CORPUS)
def test_corpus(source: str) -> None:
    assert_properties(source)


@settings(max_examples=400, suppress_health_check=list(HealthCheck), deadline=None)
@given(TEXT)
def test_properties_hold_for_generated_markdown(source: str) -> None:
    assert_properties(source)


def test_rendering_shapes() -> None:
    out = sanitize(CORPUS[0] + "\n\n" + CORPUS[3] + "\n\n" + CORPUS[2])
    assert out.startswith("# Title\n")
    assert "[a link](<http://example.com>)" in out and "title" not in out  # the title is dropped
    assert "\\[image: remote\\]" in out and "evil.example" not in out
    assert "[ok](<mailto:a@b.c>)" in out and "javascript:" in out and "](<javascript" not in out
    assert "\\<script\\>" in sanitize(CORPUS[1])
    assert sanitize(CORPUS[5]).strip() == "[ref](<http://r.example>)"  # reference definitions vanish
    assert "\\&lt;" not in sanitize(CORPUS[11]) and "\\<untrusted\\_input" in sanitize(CORPUS[11])


def test_helpers() -> None:
    assert escape_text("1. a", line_start=True) == "1\\. a"
    assert escape_text("-x", line_start=True) == "\\-x" and escape_text("-x", line_start=False) == "-x"
    assert escape_text("# a #", line_start=False, hashes=True) == "\\# a \\#"
    assert safe_href("javascript:alert(1)") is None and safe_href("relative/path") is None
    assert safe_href("https://a.example/x y|z") == "https://a.example/x%20y%7Cz"
    assert safe_href("http://a?x=1&amp;") == "http://a?x=1\\&amp;"


def test_manual_gc_gives_the_same_output_and_restores_the_collector() -> None:
    """`markdown.sanitize` runs with the cyclic GC paused per pass (C1 review: CPU and peak memory)."""
    for text in CORPUS:
        assert sanitize(text, manual_gc=True) == sanitize(text)
    assert gc.isenabled()
    gc.disable()
    try:
        sanitize(CORPUS[0], manual_gc=True)
        assert not gc.isenabled()  # a caller's disabled collector stays disabled
    finally:
        gc.enable()


_TABLE_CHILD = """
import sys, time
from noema.media.extractors.md_render import sanitize
rows = ["| a | b | c |", "|---|---|---|"] + [f"| cell {i} | value *{i}* | x_{i} |" for i in range(27_000)]
text = "\\n".join(rows) + "\\n"
started = time.process_time()
out = sanitize(text, manual_gc=True)
cpu = time.process_time() - started
# VmHWM belongs to this address space; ru_maxrss would carry the parent's high-water mark over exec
hwm_kib = next(int(line.split()[1]) for line in open("/proc/self/status") if line.startswith("VmHWM:"))
print(len(text), cpu, hwm_kib // 1024, out.startswith("| a | b | c |"))
"""


@pytest.mark.timeout(300)  # CPU time and peak RSS are measured, so a loaded host (-n 4 on 2 CPUs) only costs wall time
def test_a_1_mib_markdown_table_fits_the_markdown_limits() -> None:
    """C1 review: the C1 limits (10 s CPU, 512 MiB AS) failed a legitimate 1 MiB pipe table, whose sanitize then
    needed ~6.7 s CPU and ~460 MiB RSS (two token trees). One tree at a time with the per-pass GC: ~4.8 s and ~260
    MiB on this host, measured in a fresh `python -I` child as in the jail (docs/modules/media_pipeline.md)."""
    done = subprocess.run(
        [sys.executable, "-I", "-c", _TABLE_CHILD], capture_output=True, text=True, check=True, timeout=280
    )
    size, cpu, rss_mib, ok = done.stdout.split()
    limits = KIND_LIMITS["markdown.sanitize"]
    assert int(size) > 1024 * 1024 and ok == "True"
    assert float(cpu) < limits.cpu_s / 2
    assert int(rss_mib) < 384  # the C1 sanitizer needed ~460 MiB here
