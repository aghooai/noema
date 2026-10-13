"""MED-01 over real bytes (spec-07 §2.2): `detect` in the sandbox (InprocRlimitRunner) plus the worker's matching.
Each accepted type, polyglots (PDF+ZIP, PNG+PDF), a zip bomb named .pdf, Markdown with NUL or invalid UTF-8, empty."""

from __future__ import annotations

import struct
import zlib

import pytest

from noema.media.intake import Detected, Rejected, match_detection

from . import media_fixtures as fx
from .conftest import Extract

pytestmark = pytest.mark.unit
README = b"# Notes\n\n- buy milk\n- call bob\n\nSome **bold** text and a [link](https://example.com).\n"


def mitra_png_pdf() -> bytes:
    """PNG magic first, a PDF inside an early private chunk (both detectors say PNG)."""
    base = fx.png(16, 16)
    pdf = fx.text_pdf()
    chunk = struct.pack(">I", len(pdf)) + b"pdAt" + pdf + struct.pack(">I", zlib.crc32(b"pdAt" + pdf) & 0xFFFFFFFF)
    return base[:33] + chunk + base[33:]


async def detected(extract: Extract, data: bytes, declared: str, ext: str | None) -> Detected | Rejected:
    result = await extract("detect", data)
    if result.status == "rejected":
        return Rejected(result.rejection_code)  # type: ignore[arg-type]
    assert result.status == "ok", result
    return match_detection(result.facts, declared, ext)


@pytest.mark.parametrize(
    ("name", "declared", "ext", "row"),
    [
        ("png", "image/png", ".png", "png"),
        ("jpeg", "image/jpeg", ".jpg", "jpeg"),
        ("webp", "image/webp", ".webp", "webp"),
        ("gif", "image/gif", ".gif", "gif"),
        ("heic", "image/heic", ".heic", "heic"),
        ("pdf", "application/pdf", ".pdf", "pdf"),
        ("markdown", "text/markdown", ".md", "markdown"),
    ],
)
async def test_each_type_is_detected(extract: Extract, name: str, declared: str, ext: str, row: str) -> None:
    data = {
        "png": fx.png(),
        "jpeg": fx.jpeg(),
        "webp": fx.animated("WEBP", 1),
        "gif": fx.animated("GIF", 2),
        "heic": fx.heic(),
        "pdf": fx.text_pdf(),
        "markdown": README,
    }[name]
    result = await detected(extract, data, declared, ext)
    assert isinstance(result, Detected) and result.row.name == row


@pytest.mark.parametrize("name", ["mitra_png_pdf", "appended_png_pdf", "pdf_zip"])
async def test_polyglots_are_rejected(extract: Extract, name: str) -> None:
    data = {
        "mitra_png_pdf": mitra_png_pdf(),
        "appended_png_pdf": fx.png_pdf_polyglot(),
        "pdf_zip": fx.pdf_zip_polyglot(),
    }
    for declared, ext in (("image/png", ".png"), ("application/pdf", ".pdf")):
        assert await detected(extract, data[name], declared, ext) == Rejected("type_mismatch")


async def test_text_naming_the_pdf_header_is_not_a_polyglot(extract: Extract) -> None:
    """The hidden-PDF signal skips strict UTF-8 text without NUL (C1 review): a note quoting the header within the
    first 1024 bytes is fine. (In the first ~256 bytes libmagic itself says application/pdf: type_mismatch.)"""
    intro = b"Some notes about file formats, written down while reading the specifications. " * 5
    note = b"# PDF notes\n\n" + intro + b"\n\nEvery file starts with `%PDF-1.7` and ends with `%%EOF`.\n"
    assert 256 < note.find(b"%PDF-") < 1024
    result = await detected(extract, note, "text/markdown", ".md")
    assert isinstance(result, Detected) and result.row.name == "markdown"


async def test_a_tiny_markdown_note_is_accepted(extract: Extract) -> None:
    """Magika labels this `gitattributes`; any Magika text label is accepted for Markdown (request item 10)."""
    result = await detected(extract, b"# hi\n\nsome text\n", "text/markdown", ".md")
    assert isinstance(result, Detected) and result.row.name == "markdown"


async def test_a_zip_bomb_named_pdf_is_not_allowlisted_or_mismatched(extract: Extract) -> None:
    result = await detected(extract, fx.zip_bomb_as_pdf(), "application/pdf", ".pdf")
    assert result in (Rejected("not_allowlisted"), Rejected("type_mismatch"))


async def test_markdown_with_nul_or_invalid_utf8_is_rejected(extract: Extract) -> None:
    for data in (README + b"\x00tail", README + b"\xff\xfe broken"):
        result = await detected(extract, data, "text/markdown", ".md")
        assert isinstance(result, Rejected)


async def test_empty_and_mismatched_declarations(extract: Extract) -> None:
    assert await detected(extract, b"", "image/png", ".png") == Rejected("empty_content")
    assert await detected(extract, fx.png(), "image/jpeg", ".jpg") == Rejected("type_mismatch")
    assert await detected(extract, fx.text_pdf(), "image/png", ".png") == Rejected("type_mismatch")
