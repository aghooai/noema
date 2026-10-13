"""MED-08 (spec-07 §4.2, 02 §8.3): the negotiation matrix holds for the 7 models x {image, scanned PDF, text PDF,
PDF > 4.5 MB}, decided by `models.<alias>.modalities`; per-attempt degradations carry each attempt's model, so a
fallback from Opus to Sol reports Sol's `model_capability` only on the Sol attempt."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Final

import pytest

from noema.core.protocols.media import CONVERSE_CAPS_V1, ContentPart, FileRef
from noema.core.values import ContentBlock, DocumentBlock, ImageBlock, TextBlock
from noema.media.budget import MediaBudgetPlannerImpl
from noema.media.negotiation import MediaNegotiatorImpl

from .helpers import (
    DOC_MODELS,
    IMAGE_ONLY_MODELS,
    MODELS,
    TEXT_MODELS,
    StubFiles,
    image_file,
    model,
    page_texts,
    part,
    pdf_file,
    principal,
    text,
)

pytestmark = pytest.mark.unit
CAPS: Final = replace(CONVERSE_CAPS_V1, max_text_tokens=50_000)
SCANNED_MD: Final = ["", ""]
SCANNED_OCR: Final = ["scanned page one", "scanned page two"]
TEXT_MD: Final = ["first page text", "second page text"]


def shape(blocks: Sequence[ContentBlock]) -> list[str]:
    """A compact view of the blocks: label lines verbatim, `untrusted:<kind>`, `image`, `document:<name>`."""
    out = []
    for b in blocks:
        if isinstance(b, TextBlock):
            out.append(b.text.split('kind="')[1].split('"')[0] if b.text.startswith("<untrusted_input") else b.text)
        elif isinstance(b, ImageBlock):
            out.append("image")
        else:
            assert isinstance(b, DocumentBlock)
            out.append(f"document:{b.name}")
    return out


def cell(files: StubFiles, content: str) -> FileRef:
    if content == "image":
        return image_file(files, "f_1")
    if content == "scanned_pdf":
        return pdf_file(files, "f_1", SCANNED_MD, ocr=SCANNED_OCR)
    return pdf_file(files, "f_1", TEXT_MD, sanitized_bytes=4_600_000 if content == "big_pdf" else 100_000)


PAGES: Final = ["[doc-1 page 1]", "image", "[doc-1 page 2]", "image"]
EXPECTED: Final[dict[tuple[str, str], tuple[list[str], list[tuple[str, str]]]]] = {}
for alias in MODELS:
    if alias in TEXT_MODELS:
        EXPECTED[alias, "image"] = (["ocr_text"], [("model_capability", "ocr_text")])
        EXPECTED[alias, "scanned_pdf"] = (["ocr_text"], [("model_capability", "ocr_text")])
        EXPECTED[alias, "text_pdf"] = (["markdown"], [("model_capability", "markdown")])
        EXPECTED[alias, "big_pdf"] = (["markdown"], [("model_capability", "markdown")])
        continue
    EXPECTED[alias, "image"] = (["[image-1]", "image"], [])
    derived = ["markdown", *PAGES]
    if alias in DOC_MODELS:
        EXPECTED[alias, "scanned_pdf"] = (["[doc-1]", "document:doc-1", *PAGES], [])
        EXPECTED[alias, "text_pdf"] = (["[doc-1]", "document:doc-1"], [])
        EXPECTED[alias, "big_pdf"] = (derived, [("size_limit", "markdown+page_images(2)")])
    else:
        EXPECTED[alias, "scanned_pdf"] = (PAGES, [("model_capability", "markdown+page_images(2)")])
        EXPECTED[alias, "text_pdf"] = (derived, [("model_capability", "markdown+page_images(2)")])
        EXPECTED[alias, "big_pdf"] = (derived, [("model_capability", "markdown+page_images(2)")])


def test_matrix_covers_every_model_and_cell() -> None:
    assert set(DOC_MODELS) | set(IMAGE_ONLY_MODELS) | set(TEXT_MODELS) == set(MODELS) and len(EXPECTED) == 28
    for alias in DOC_MODELS:
        assert {"image", "document"} <= model(alias).modalities
    for alias in IMAGE_ONLY_MODELS:
        assert "image" in model(alias).modalities and "document" not in model(alias).modalities
    for alias in TEXT_MODELS:
        assert model(alias).modalities == {"text"}


@pytest.mark.parametrize(("alias", "content"), sorted(EXPECTED))
async def test_negotiation_matrix(alias: str, content: str) -> None:
    files = StubFiles()
    ref = cell(files, content)
    md = SCANNED_MD if content == "scanned_pdf" else TEXT_MD
    texts = None if content == "image" else page_texts(md, SCANNED_OCR if content == "scanned_pdf" else None)
    parts: list[ContentPart] = [text(0, "Describe it."), part(1, ref, texts=texts)]
    plan = MediaBudgetPlannerImpl().plan(parts, [], CAPS)
    blocks, degradations = await MediaNegotiatorImpl(files.service()).negotiate(
        alias, model(alias), parts, plan, principal=principal()
    )
    expected_shape, expected_degradations = EXPECTED[alias, content]
    assert shape(blocks) == expected_shape
    assert [(d.reason, d.to) for d in degradations] == expected_degradations
    for d in degradations:
        assert (d.from_, d.file_id, d.part_index, d.model, d.attempt_id) == (
            ref.media_type,
            "f_1",
            1,
            model(alias).id,
            None,
        )
    assert plan.degradations == ()


async def test_fallback_from_opus_to_sol_renegotiates() -> None:
    """MED-08, attempt half (partial): every attempt re-negotiates, and its degradations carry that attempt's model.
    `negotiate` has no attempt id (spec-02 §6), so `attempt_id` stays None for the caller to stamp; that stamping is
    covered with the prompt builder and generation (C2)."""
    files = StubFiles()
    parts = [text(0, "Summarize."), part(1, pdf_file(files, "f_doc", TEXT_MD), texts=TEXT_MD)]
    plan = MediaBudgetPlannerImpl().plan(parts, [], CAPS)
    negotiator = MediaNegotiatorImpl(files.service())
    attempts = {}
    for alias in ("opus-5.5", "sol-6.1"):
        blocks, found = await negotiator.negotiate(alias, model(alias), parts, plan, principal=principal())
        attempts[alias] = (shape(blocks), [(d.reason, d.to, d.model, d.attempt_id) for d in found])
    assert attempts["opus-5.5"] == (["[doc-1]", "document:doc-1"], [])
    assert attempts["sol-6.1"] == (
        ["markdown", *PAGES],
        [("model_capability", "markdown+page_images(2)", "us.openai.gpt-6.1-sol", None)],
    )
