"""`detect` (ADR-002 §5, spec-07 §2.2): Magika in HIGH_CONFIDENCE mode and libmagic (`mime=True`) on the whole
file, plus a strict UTF-8 decode and a NUL scan. Facts only; the worker matches them against the equivalence map.

Polyglot signals the two detectors can both miss (MED-01: PDF+ZIP, PNG+PDF) reject the file as `type_mismatch`
here: a `%PDF-` header inside the first 1024 bytes behind other (non-whitespace) bytes (PDF readers accept it there,
mitra-style PNG+PDF), or a ZIP end-of-central-directory record in the last 65,557 bytes (no Phase 1 type is a ZIP).
The hidden-PDF signal does not apply to strict UTF-8 text without NUL bytes: such a file can only be accepted as
Markdown, which is re-rendered as text, and a note that mentions `%PDF-1.7` near its start is legitimate."""

from __future__ import annotations

import codecs
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from noema.core.values import JsonValue
from noema.media.extractors.common import Outputs, Rejection, version_of

EXTRACTOR: Final = f"{version_of('magika')}+{version_of('python-magic')}"
_CHUNK: Final = 1024 * 1024


def text_facts(path: Path) -> tuple[int, bool, bool]:
    """(bytes, utf8_strict, has_nul) of a file, read in 1 MiB chunks."""
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    size, strict, nul = 0, True, False
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            size += len(chunk)
            nul = nul or b"\x00" in chunk
            if strict:
                try:
                    decoder.decode(chunk)
                except UnicodeDecodeError:
                    strict = False
        if strict:
            try:
                decoder.decode(b"", final=True)
            except UnicodeDecodeError:
                strict = False
    return size, strict, nul


PDF_WINDOW: Final = 1024
ZIP_EOCD: Final = b"PK\x05\x06"
ZIP_EOCD_WINDOW: Final = 65_557  # 22-byte record + the maximal 65,535-byte comment


def polyglot(path: Path, size: int, *, text: bool = False) -> bool:
    """True when the file carries a second format's header or trailer (see the module docstring); `text`: the file
    is strict UTF-8 without NUL (no hidden-PDF check)."""
    with path.open("rb") as fh:
        head = fh.read(PDF_WINDOW + 5)
        fh.seek(max(0, size - ZIP_EOCD_WINDOW))
        tail = fh.read(ZIP_EOCD_WINDOW)
    at = head.find(b"%PDF-")
    hidden_pdf = not text and at > 0 and bool(head[:at].strip())  # leading whitespace alone: a sloppy plain PDF
    return hidden_pdf or ZIP_EOCD in tail


def run(input_path: Path, out: Outputs, params: Mapping[str, JsonValue]) -> None:
    """Fill the detect facts (no outputs); a polyglot signal is a `type_mismatch` rejection."""
    import magic
    from magika import Magika, PredictionMode

    size, strict, nul = text_facts(input_path)
    if size and polyglot(input_path, size, text=strict and not nul):
        raise Rejection("type_mismatch")
    result = Magika(prediction_mode=PredictionMode.HIGH_CONFIDENCE).identify_path(input_path)
    mime = magic.from_file(str(input_path), mime=True) if size else "application/x-empty"
    out.facts.update(
        {
            "magika_label": str(result.output.label)[:64],
            "magika_score": round(min(max(float(result.score), 0.0), 1.0), 6),
            "libmagic_mime": str(mime)[:128],
            "bytes": size,
            "utf8_strict": strict,
            "has_nul": nul,
        }
    )
