"""M3 (06 §10, spec-10 §3 "OCR CER on the probe fixtures, Tesseract only; meas"): `make qa-ocr-cer`, inside the
media image (`make test-media MEDIA_TESTS=tests/qa/media`: Tesseract 5 + eng, network none).

Every fixture goes through the shipped `ocr` extractor (`noema.media.extractors.ocr.run`, the sandbox job's code):
the probe fixtures (`number.png` reads 4471; `invoice.pdf` page 1 rendered at 300 dpi, as for a page without a text
layer) and a few generated images (Pillow's bundled scalable font, so the renders are the same on every host).
CER = Levenshtein(reference, hypothesis) / len(reference) after collapsing whitespace runs (case-sensitive).

Pass: every fixture recognized and its CER reported (a `[gate] M3` line with the per-fixture and aggregate CER);
regression guard on the probe fixtures and the clean renders: every CER <= 0.10 and their aggregate <= 0.05. Four
degraded renders (14 px, noise, 3 degree rotation, half-scale JPEG) are reported without a bound. No engine
comparison (Tesseract only).
"""

from __future__ import annotations

import io
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Final

import pytest

from noema.core.values import JsonValue

PROBES: Final = Path("/codes/aghooai/probes/fixtures")
MAX_CER: Final = 0.10
MAX_AGGREGATE_CER: Final = 0.05


def levenshtein(a: str, b: str) -> int:
    """Edit distance (insertions, deletions, substitutions; each costs 1)."""
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def normalized(text: str) -> str:
    return " ".join(text.split())


def cer(reference: str, hypothesis: str) -> tuple[int, int]:
    """(edits, reference characters) after whitespace normalization."""
    ref, hyp = normalized(reference), normalized(hypothesis)
    return levenshtein(ref, hyp), len(ref)


def test_cer_arithmetic() -> None:
    assert levenshtein("kitten", "sitting") == 3
    assert cer("Invoice  4471\n", "Invoice 4471") == (0, 12)
    assert cer("4471", "4471 -") == (2, 4)
    assert cer("abc", "") == (3, 3)


def render(lines: list[str], size: int, fg: int = 0, bg: int = 255, degrade: str = "") -> bytes:
    """A grayscale PNG of `lines` in Pillow's bundled font at `size` px, 40 px margins; `degrade`: `noise` (seeded
    uniform +/-60 per pixel), `rotate` (3 degrees, white fill) or `jpeg` (half scale, JPEG quality 30)."""
    import random

    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.load_default(size=size)
    width = int(max(font.getlength(line) for line in lines)) + 80
    height = int(len(lines) * size * 1.5) + 80
    image = Image.new("L", (width, height), bg)
    draw = ImageDraw.Draw(image)
    for i, line in enumerate(lines):
        draw.text((40, 40 + int(i * size * 1.5)), line, fill=fg, font=font)
    if degrade == "noise":
        rnd = random.Random(7)
        image = Image.frombytes(
            "L", image.size, bytes(min(255, max(0, p + rnd.randint(-60, 60))) for p in image.tobytes())
        )
    elif degrade == "rotate":
        image = image.rotate(3, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=bg)
    elif degrade == "jpeg":
        small = image.resize((width // 2, height // 2), Image.Resampling.BILINEAR)
        lossy = io.BytesIO()
        small.save(lossy, format="JPEG", quality=30)
        image = Image.open(io.BytesIO(lossy.getvalue())).convert("L")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


PANGRAM: Final = ["The quick brown fox jumps over the lazy dog.", "Pack my box with five dozen liquor jugs 0123456789."]
INVOICE: Final = ["Invoice 4471", "Total due: 128.40 EUR", "Due date: 2026-11-01", "Charged twice on 2026-10-03."]
NOTICE: Final = ["Please reply before Friday with the order number,", "the delivery address and a phone number."]
Fixture = tuple[str, str, Callable[[Path], Path], dict[str, JsonValue]]


def probe(name: str) -> Callable[[Path], Path]:
    def make(_tmp: Path) -> Path:
        path = PROBES / name
        if not path.is_file():
            pytest.skip(f"probe fixture {path} missing (make test-media mounts PROBE_FIXTURES)")
        return path

    return make


def generated(
    name: str, lines: list[str], size: int, fg: int = 0, bg: int = 255, degrade: str = ""
) -> Callable[[Path], Path]:
    def make(tmp: Path) -> Path:
        path = tmp / name
        path.write_bytes(render(lines, size, fg, bg, degrade))
        return path

    return make


FIXTURES: Final[list[Fixture]] = [
    ("probe number.png", "4471", probe("number.png"), {}),
    ("probe invoice.pdf p1 @300dpi", "Invoice number 4471 - charged twice", probe("invoice.pdf"), {"pages": [1]}),
    ("generated pangram 32px", "\n".join(PANGRAM), generated("pangram.png", PANGRAM, 32), {}),
    ("generated invoice 28px", "\n".join(INVOICE), generated("invoice.png", INVOICE, 28), {}),
    ("generated notice 24px gray", "\n".join(NOTICE), generated("notice.png", NOTICE, 24, 70, 225), {}),
]
# reported only (no bound): how the same engine degrades on harder renders
DEGRADED: Final[list[Fixture]] = [
    ("degraded notice 14px", "\n".join(NOTICE), generated("small.png", NOTICE, 14), {}),
    ("degraded invoice 28px noise", "\n".join(INVOICE), generated("noise.png", INVOICE, 28, degrade="noise"), {}),
    ("degraded pangram 32px rotated 3deg", "\n".join(PANGRAM), generated("rot.png", PANGRAM, 32, degrade="rotate"), {}),
    (
        "degraded invoice 28px half-scale jpeg q30",
        "\n".join(INVOICE),
        generated("jpg.png", INVOICE, 28, degrade="jpeg"),
        {},
    ),
]


@pytest.mark.timeout(600)
def test_ocr_cer(tmp_path: Path) -> None:
    if shutil.which("tesseract") is None:
        pytest.skip("no tesseract: M3 runs in the media image (make qa-ocr-cer)")
    from noema.media.extractors.common import Outputs
    from noema.media.extractors.ocr import EXTRACTOR, run

    rows: list[dict[str, object]] = []
    cers: list[float] = []
    edits_total = chars_total = 0
    for i, (name, reference, make, params) in enumerate(FIXTURES + DEGRADED):
        source = make(tmp_path)
        out_dir = tmp_path / f"out-{i}"
        out_dir.mkdir()
        run(source, Outputs(kind="ocr", out_dir=out_dir, extractor=EXTRACTOR), params)
        hypothesis = (out_dir / "ocr_text.txt").read_text("utf-8")
        edits, chars = cer(reference, hypothesis)
        rows.append({"fixture": name, "cer": round(edits / chars, 4), "edits": edits, "chars": chars,
                     "hypothesis": normalized(hypothesis)})  # fmt: skip
        if i < len(FIXTURES):
            edits_total, chars_total = edits_total + edits, chars_total + chars
            cers.append(round(edits / chars, 4))
    aggregate = round(edits_total / chars_total, 4)
    print("\n[gate] M3 " + json.dumps({"extractor": EXTRACTOR, "aggregate_cer": aggregate, "fixtures": rows}))
    assert max(cers) <= MAX_CER, rows
    assert aggregate <= MAX_AGGREGATE_CER, rows
