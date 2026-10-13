"""MED-08 for `InputViewResolver` (spec-07 §5, spec-06 §4): the same 7 models x {image, scanned PDF, text PDF,
PDF > 4.5 MB} under both modality policies (MED-10 `skip_incapable_members` cells)."""

from __future__ import annotations

from typing import Final

import pytest

from noema.core.protocols.media import VIEW_BUDGET_V1, ViewKind
from noema.core.values import DocumentBlock, ImageBlock, TextBlock
from noema.media.views import InputViewResolverImpl

from .helpers import DOC_MODELS, MODELS, TEXT_MODELS, StubFiles, member, model, part, principal, text
from .test_negotiation_matrix import SCANNED_OCR, TEXT_MD, cell

pytestmark = pytest.mark.unit
CELLS: Final = ("image", "scanned_pdf", "text_pdf", "big_pdf")


def expected(alias: str, content: str) -> tuple[ViewKind, list[str], list[str]]:
    """(part view kind, block types, degradation reasons) under derive_for_text_members."""
    doc, text_only = alias in DOC_MODELS, alias in TEXT_MODELS
    if content == "image":
        return ("ocr_text", ["text"], []) if text_only else ("native", ["image"], [])
    if content == "scanned_pdf":
        if doc:
            return "native", ["document"], []
        return ("ocr_text", ["text"], []) if text_only else ("derived_text", ["image", "image"], [])
    if content == "text_pdf" and doc:
        return "native", ["document"], []
    return "derived_text", ["text"], ["size_limit"] if content == "big_pdf" and doc else []


def kinds(blocks: tuple[object, ...]) -> list[str]:
    names = {TextBlock: "text", ImageBlock: "image", DocumentBlock: "document"}
    return [names[type(b)] for b in blocks]


@pytest.mark.parametrize("content", CELLS)
@pytest.mark.parametrize("alias", MODELS)
async def test_view_matrix(alias: str, content: str) -> None:
    files = StubFiles()
    parts = [text(0, "Which department?"), part(1, cell(files, content), "image-1" if content == "image" else "doc-1")]
    resolver = InputViewResolverImpl(files.service())
    view = await resolver.resolve(
        member(alias),
        parts,
        policy="derive_for_text_members",
        budget=VIEW_BUDGET_V1,
        model=model(alias),
        principal=principal(),
    )
    kind, blocks, reasons = expected(alias, content)
    media = view.parts[1]
    assert (media.kind, kinds(media.blocks), [d.reason for d in view.degradations]) == (kind, blocks, reasons)
    assert view.parts[0].kind == "native" and view.parts[0].blocks == (TextBlock("Which department?"),)
    modality = "image" if content == "image" else "document"
    assert view.by_modality == {"text": "native", modality: kind} and view.kind == kind
    if kind == "ocr_text" and content == "scanned_pdf":
        assert media.blocks == (TextBlock("\n\n".join(SCANNED_OCR)),)
    if kind == "derived_text" and content != "scanned_pdf":
        assert media.blocks[0] == TextBlock("\n\n".join(TEXT_MD))
    if isinstance(media.blocks[0], DocumentBlock):
        assert media.blocks[0].name == "doc-1" and media.blocks[0].pages == 2


@pytest.mark.parametrize("content", CELLS)
@pytest.mark.parametrize("alias", MODELS)
async def test_skip_incapable_members(alias: str, content: str) -> None:
    files = StubFiles()
    parts = [part(0, cell(files, content))]
    view = await InputViewResolverImpl(files.service()).resolve(
        member(alias),
        parts,
        policy="skip_incapable_members",
        budget=VIEW_BUDGET_V1,
        model=model(alias),
        principal=principal(),
    )
    degraded = expected(alias, content)[0] == "ocr_text"  # images for text models; scanned PDFs for text models
    assert view.kind == ("skipped" if degraded else expected(alias, content)[0])
    if degraded:
        assert view.parts[0].kind == "skipped" and view.parts[0].blocks == ()
        assert all(key[1] in ("markdown", "ocr_text") for key in files.reads)  # no binary read for a skipped member
