"""`ocr` (ADR-002 §5, spec-07 §1): Tesseract 5 (`tesseract - stdout -l eng --psm 3`) as a child process.

Input `raster` (an image; `params.pages` null) → one text segment. Input `pdf_sanitized` (`params.pages` = the
pages without a text layer, ascending) → pypdfium2 renders each listed page at 300 dpi; the output holds one segment
per document page, `\\f`-separated, empty for the pages not listed. Output `ocr_text.txt` (strict UTF-8, no NUL, no
form feed inside a segment); no facts.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from noema.core.values import JsonValue
from noema.media.extractors.common import Outputs, version_of

TESSERACT_ARGS: Final = ("-", "stdout", "-l", "eng", "--psm", "3", "-c", "page_separator=")
PDF_DPI: Final = 300
PAGE_TIMEOUT_S: Final = 120
EXTRACTOR: Final = f"tesseract+{version_of('pypdfium2')}"


class OcrUnavailable(RuntimeError):
    """No tesseract binary on PATH."""


def tesseract_path() -> str:
    """The tesseract binary; `OcrUnavailable` when missing."""
    found = shutil.which("tesseract", path=os.environ.get("PATH") or "/usr/bin:/usr/local/bin")
    if found is None:
        raise OcrUnavailable("tesseract is not installed")
    return found


def clean_segment(text: str) -> str:
    """OCR text without form feeds or C0 controls other than tab and newline, trailing whitespace trimmed."""
    kept = "".join(ch if ch in "\t\n" or ord(ch) >= 32 else " " for ch in text.replace("\f", "\n"))
    return kept.replace("\x7f", " ").rstrip()


def recognize(png: bytes, *, binary: str) -> str:
    """Tesseract over one image given as bytes on stdin."""
    env = {"PATH": "/usr/bin:/usr/local/bin", "OMP_THREAD_LIMIT": "1", "LANG": "C.UTF-8"}
    if prefix := os.environ.get("TESSDATA_PREFIX"):
        env["TESSDATA_PREFIX"] = prefix
    proc = subprocess.run(  # noqa: S603 (fixed argv, input on stdin)
        [binary, *TESSERACT_ARGS],
        input=png,
        capture_output=True,
        timeout=PAGE_TIMEOUT_S,
        check=False,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"tesseract exited {proc.returncode}")
    return clean_segment(proc.stdout.decode("utf-8", errors="replace"))


def _pdf_segments(input_path: Path, pages: Sequence[int], binary: str) -> list[str]:
    import io

    from noema.media.extractors.pdf_render import open_document

    doc = open_document(input_path)
    try:
        segments = [""] * len(doc)
        for number in pages:
            if not 1 <= number <= len(doc):
                continue
            page = doc[number - 1]
            try:
                bitmap = page.render(scale=PDF_DPI / 72, may_draw_forms=False)
                try:
                    buf = io.BytesIO()
                    bitmap.to_pil().convert("L").save(buf, format="PNG")
                finally:
                    bitmap.close()
            finally:
                page.close()
            segments[number - 1] = recognize(buf.getvalue(), binary=binary)
        return segments
    finally:
        doc.close()


def run(input_path: Path, out: Outputs, params: Mapping[str, JsonValue]) -> None:
    """OCR one image or the listed pages of a PDF (see the module docstring)."""
    binary = tesseract_path()
    pages = params.get("pages")
    if isinstance(pages, list):
        segments = _pdf_segments(input_path, [p for p in pages if isinstance(p, int)], binary)
    else:
        segments = [recognize(input_path.read_bytes(), binary=binary)]
    text = "\f".join(segments)
    out.add("ocr_text.txt", text.encode("utf-8"), kind="ocr_text", media_type="text/plain")
