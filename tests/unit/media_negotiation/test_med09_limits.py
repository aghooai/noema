"""MED-09 (spec-07 §7, spec-10 M5): 21 images + 6 PDFs + a 600-page PDF, planned once and negotiated for every model
of the matrix, never exceed the Converse limits (checked here by a local enforcer of 02 §8.4; the wire-mock run is
C2/E's), with the expected `media_budget` degradations; a lone 600-page PDF gets term-selected page images."""

from __future__ import annotations

from dataclasses import replace
from typing import Final

import pytest

from noema.core.protocols.media import CONVERSE_CAPS_V1, ContentPart
from noema.core.values import ImageBlock, TextBlock
from noema.media.budget import MediaBudgetPlannerImpl
from noema.media.negotiation import MediaNegotiatorImpl

from .helpers import (
    DOC_MODELS,
    IMAGE_ONLY_MODELS,
    MODELS,
    PNG,
    StubFiles,
    blob,
    converse_violations,
    image_file,
    model,
    page_texts,
    part,
    pdf_file,
    principal,
    text,
)

pytestmark = pytest.mark.unit
PLANNER: Final = MediaBudgetPlannerImpl()
CAPS: Final = replace(CONVERSE_CAPS_V1, max_text_tokens=100_000)


def long_pdf(files: StubFiles, fid: str, n: int = 600) -> tuple[ContentPart, list[str]]:
    """A 600-page report: ~3,000 chars of text per page, every 7th page scanned (OCR only), "warranty" on page 450."""
    md = [("" if p % 7 == 0 else f"Page {p} body. " + "lorem ipsum " * 250) for p in range(1, n + 1)]
    md[449] += " The warranty clause."
    ocr = [(f"scanned {p}" if p % 7 == 0 else "") for p in range(1, n + 1)]
    ref = pdf_file(files, fid, md, sanitized_bytes=4_400_000, ocr=ocr, page_image_bytes=180_000)
    return part(0, ref, texts=page_texts(md, ocr)), md


def big_turn(files: StubFiles) -> list[ContentPart]:
    report, _ = long_pdf(files, "f_report")
    parts: list[ContentPart] = [text(0, "Which warranty applies to the shipment?"), replace(report, index=1)]
    parts += [part(2 + i, image_file(files, f"f_img{i}", raster_bytes=600_000)) for i in range(21)]
    for j in range(6):
        md = [f"contract {j} page {p} " * 40 for p in range(1, 9)]
        parts.append(part(23 + j, pdf_file(files, f"f_pdf{j}", md, sanitized_bytes=1_200_000), texts=md))
    return parts


async def test_every_model_stays_within_the_converse_limits() -> None:
    files = StubFiles()
    parts = big_turn(files)
    plan = PLANNER.plan(parts, [], CAPS)
    # images first (step 1.1): 20 slots of 800,000 B base64 = 16 MB of the 18 MB binary budget; the 21st has no slot.
    # PDFs (1.2): the 4.4 MB report no longer fits, one 1.2 MB PDF does, the other five are cut; no image slot is
    # left for page images (1.3); the seven PDFs share the 100,000-token text allowance (2).
    assert [p.native for p in plan.parts if p.kind == "image"] == [True] * 20 + [False]
    assert [(p.label, p.native, p.page_images) for p in plan.parts if p.kind == "pdf"] == [
        ("doc-1", False, ()),
        ("doc-2", True, ()),
        *((f"doc-{k}", False, ()) for k in range(3, 8)),
    ]
    assert [(d.file_id, d.to) for d in plan.degradations] == [
        ("f_img20", "ocr_text"),
        ("f_report", "markdown"),
        *((f"f_pdf{k}", "markdown") for k in range(1, 6)),
        ("f_report", "markdown(pages 1-21 of 600)"),
    ]
    assert all(d.reason == "media_budget" for d in plan.degradations)
    pdfs = ["f_report", *(f"f_pdf{k}" for k in range(6))]
    negotiator = MediaNegotiatorImpl(files.service())
    for alias in MODELS:
        blocks, degradations = await negotiator.negotiate(alias, model(alias), parts, plan, principal=principal())
        assert converse_violations(blocks) == [], alias
        # per attempt: capability only (the plan's cuts are reported once, on preflight.completed)
        expected = [] if alias in DOC_MODELS else [(f, "model_capability", "markdown") for f in pdfs]
        if alias not in DOC_MODELS + IMAGE_ONLY_MODELS:
            expected[1:1] = [(f"f_img{i}", "model_capability", "ocr_text") for i in range(21)]
        assert [(d.file_id, d.reason, d.to) for d in degradations] == expected, alias
        assert all(d.model == model(alias).id and d.attempt_id is None for d in degradations)
        if "image" not in model(alias).modalities:
            assert not any(isinstance(b, ImageBlock) for b in blocks)


async def test_a_600_page_pdf_alone() -> None:
    files = StubFiles()
    report, md = long_pdf(files, "f_report")
    parts = [text(0, "Is there a warranty clause?"), replace(report, index=1)]
    plan = PLANNER.plan(parts, [], CAPS)
    (planned,) = plan.parts
    assert planned.native and len(planned.page_images) == 20
    assert planned.page_images[:3] == (1, 2, 7) and 450 in planned.page_images  # 1-2, the term page, then scanned
    assert planned.text_cut is not None and planned.text_cut.endswith(" of 600")
    assert [d.reason for d in plan.degradations] == ["media_budget"]
    negotiator = MediaNegotiatorImpl(files.service())
    for alias in MODELS:
        blocks, _ = await negotiator.negotiate(alias, model(alias), parts, plan, principal=principal())
        assert converse_violations(blocks) == [], alias
        body = "".join(b.text for b in blocks if isinstance(b, TextBlock) and b.text.startswith("<untrusted_input"))
        assert len(body) <= CAPS.max_text_tokens * 4 + 200  # the derived text stays within the allowance
        images = sum(isinstance(b, ImageBlock) for b in blocks)
        if alias in ("luna-6", "sol-6.1"):
            assert images == 20 and "[doc-1 page 450]" in [b.text for b in blocks if isinstance(b, TextBlock)]
        elif "document" in model(alias).modalities:
            assert images == len([p for p in planned.page_images if p % 7 == 0])  # scanned pages only
    assert md[0].startswith("Page 1 body.")


async def test_page_images_of_uneven_size_stay_within_the_payload() -> None:
    # regression: the plan prices page images at the average page size; a 3.7 MB cover and 1 MB scans in a PDF of
    # 20 KB pages made every image or document model exceed 20 MB (90-98 MB requests)
    files = StubFiles()
    md = [("" if p % 30 == 0 else f"page {p} text") for p in range(1, 601)]  # 20 scanned pages: 30, 60, ..., 600
    ref = pdf_file(files, "f_doc", md, sanitized_bytes=1_000_000, page_image_bytes=20_000)
    sizes = {p: 3_700_000 if p == 1 else 1_000_000 if p % 30 == 0 else 20_000 for p in range(1, 601)}
    files.data.update({("f_doc", "page_images", p): blob(PNG, n) for p, n in sizes.items()})
    honest = tuple(replace(d, bytes=sum(sizes.values())) if d.kind == "page_images" else d for d in ref.derivatives)
    parts = [text(0, "Summarize"), part(1, replace(ref, derivatives=honest), texts=page_texts(md))]
    plan = PLANNER.plan(parts, [], replace(CAPS, max_text_tokens=50_000))
    (planned,) = plan.parts
    assert planned.native and planned.page_images == (1, 2, *range(30, 541, 30))
    # reserved: 20 pages at max(average 58,800 B, the 65,536 B floor) = 20 x 87,384 B of base64 = 1,747,680 B
    negotiator = MediaNegotiatorImpl(files.service())
    for alias in MODELS:
        blocks, degradations = await negotiator.negotiate(alias, model(alias), parts, plan, principal=principal())
        assert converse_violations(blocks) == [], alias
        labels = [b.text for b in blocks if isinstance(b, TextBlock) and b.text.startswith("[doc-1 page")]
        found = [(d.reason, d.to) for d in degradations]
        if alias in DOC_MODELS:  # the scanned pages only: page 30 fits, the 17 others would overrun
            assert labels == ["[doc-1 page 30]"]
            assert found == [("size_limit", "native+page_images(1)")]
        elif alias in IMAGE_ONLY_MODELS:  # the 3.7 MB cover would overrun alone; pages 2 and 30 fit
            assert labels == ["[doc-1 page 2]", "[doc-1 page 30]"]
            assert found == [("model_capability", "markdown+page_images(2)"), ("size_limit", "markdown+page_images(2)")]
        else:
            assert labels == [] and found == [("model_capability", "markdown")]
