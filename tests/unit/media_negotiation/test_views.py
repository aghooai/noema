"""MED-10 (spec-07 §5, spec-06 §4.4, IMPL-058, IMPL-075): `metadata_only` views, the `VIEW_BUDGET_V1` cut rule and
its `note` strings, adequacy per modality (`by_modality`, `kind`), JSON parts and read failures."""

from __future__ import annotations

from dataclasses import replace
from typing import Literal

import pytest

from noema.core.protocols.media import VIEW_BUDGET_V1, ContentPart, DecisionViewBudget, InputView
from noema.core.values import DocumentBlock, ImageBlock, TextBlock
from noema.media.views import InputViewResolverImpl, least, rank, summary_kind

from .helpers import PNG, StubFiles, image_file, markdown_file, member, model, part, pdf_file, principal, text

pytestmark = pytest.mark.unit
Policy = Literal["derive_for_text_members", "skip_incapable_members"]


async def resolve(
    files: StubFiles,
    alias: str,
    parts: list[ContentPart],
    *,
    budget: DecisionViewBudget = VIEW_BUDGET_V1,
    policy: Policy = "derive_for_text_members",
) -> InputView:
    return await InputViewResolverImpl(files.service()).resolve(
        member(alias), parts, policy=policy, budget=budget, model=model(alias), principal=principal()
    )


def test_adequacy_ranks_and_summary() -> None:
    assert [rank("image", k) for k in ("native", "ocr_text", "thumbnail", "metadata_only", "skipped")] == [
        2,
        1,
        1,
        0,
        0,
    ]
    assert [rank("document", k) for k in ("native", "derived_text", "ocr_text", "metadata_only")] == [2, 2, 1, 0]
    assert (
        least("document", ["native", "derived_text"]) == "derived_text"
        and least("image", ["native", "ocr_text"]) == "ocr_text"
    )
    assert summary_kind({"text": "native"}) == "native"
    assert summary_kind({"image": "native", "document": "derived_text"}) == "native"  # tie: image first
    assert summary_kind({"image": "native", "document": "ocr_text"}) == "ocr_text"
    assert summary_kind({"image": "ocr_text", "document": "metadata_only"}) == "metadata_only"


@pytest.mark.parametrize(("status", "reason"), [("failed", "extraction_failed"), ("pending", "not_ready")])
async def test_missing_markdown_gives_metadata_only(status: Literal["pending", "failed"], reason: str) -> None:
    files = StubFiles()
    ref = pdf_file(files, "f_p", ["text"])
    ref = replace(
        ref, derivatives=tuple(replace(d, status=status) if d.kind == "markdown" else d for d in ref.derivatives)
    )
    view = await resolve(files, "luna-6", [part(0, ref)])  # Luna alone, as in lightweight-model-decide
    assert view.kind == "metadata_only" and view.parts[0].blocks == ()
    assert [(d.reason, d.to, d.file_id, d.part_index) for d in view.degradations] == [(reason, "omitted", "f_p", 0)]
    skipped = await resolve(files, "luna-6", [part(0, ref)], policy="skip_incapable_members")
    assert skipped.kind == "skipped" and skipped.by_modality["document"] == "skipped"


async def test_empty_ocr_is_metadata_only_and_rejected_files_report_rejected() -> None:
    files = StubFiles()
    empty = await resolve(files, "deepseek-3.2", [part(0, image_file(files, "f_i", ocr=" \n"))])
    assert empty.kind == "metadata_only" and empty.degradations == ()
    rejected = await resolve(files, "haiku-5.5", [part(0, image_file(files, "f_r", status="rejected"))])
    assert rejected.kind == "metadata_only" and [d.reason for d in rejected.degradations] == ["rejected"]


async def test_text_budget_truncates_at_page_boundaries() -> None:
    files = StubFiles()
    md = ["w" * 396] * 600  # 99 tokens per page; 100 with the page separator
    long = part(0, pdf_file(files, "f_long", md), "doc-1")
    note = part(1, markdown_file(files, "f_md", "n" * 40), "doc-2")
    view = await resolve(files, "deepseek-3.2", [long, note])
    first, second = view.parts
    # doc-1's share is floor(6000 / 2) = 3000 tokens: 30 pages = 30*396 + 29*2 = 11,938 chars = 2,985 tokens
    assert (first.kind, first.truncated, first.note) == ("derived_text", True, "pages 1-30 of 600")
    assert first.blocks == (TextBlock("\n\n".join(md[:30])),)
    assert (second.truncated, second.note, second.blocks) == (False, None, (TextBlock("n" * 40),))
    assert [(d.reason, d.to) for d in view.degradations] == [("media_budget", "markdown(pages 1-30 of 600)")]
    assert view.by_modality["document"] == "derived_text"  # a cut never lowers adequacy


async def test_partial_page_note() -> None:
    files = StubFiles()
    page = ("x" * 99 + "\n") * 300  # 30,000 chars on one page
    view = await resolve(files, "glm-5.3", [part(0, pdf_file(files, "f_p", [page]))])
    (only,) = view.parts
    assert (only.truncated, only.note) == (True, "page 1 of 1, partial")
    assert only.blocks == (TextBlock(page[: page.rfind("\n", 0, 24_001)]),)
    assert [d.to for d in view.degradations] == ["markdown(page 1 of 1, partial)"]


async def test_documents_beyond_five_get_derived_text() -> None:
    files = StubFiles()
    parts = [part(i, pdf_file(files, f"f_{i}", [f"doc {i}"]), f"doc-{i + 1}") for i in range(6)]
    view = await resolve(files, "haiku-5.5", parts)
    assert [p.kind for p in view.parts] == ["native"] * 5 + ["derived_text"]
    assert isinstance(view.parts[0].blocks[0], DocumentBlock) and view.parts[5].blocks == (TextBlock("doc 5"),)
    assert [(d.reason, d.to, d.file_id) for d in view.degradations] == [("media_budget", "markdown", "f_5")]
    assert view.by_modality == {"document": "native"} and view.kind == "native"


async def test_images_and_page_images_share_twenty_slots() -> None:
    files = StubFiles()
    images = [part(i, image_file(files, f"f_{i}"), f"image-{i + 1}") for i in range(19)]
    scanned = part(19, pdf_file(files, "f_s", ["", "", ""], text_layer=()), "doc-1")
    view = await resolve(files, "sol-6.1", [*images, scanned])
    assert [p.kind for p in view.parts] == ["native"] * 19 + ["derived_text"]
    assert sum(isinstance(b, ImageBlock) for b in view.parts[19].blocks) == 1
    assert [(d.reason, d.to) for d in view.degradations] == [("media_budget", "markdown+page_images(1)")]


async def test_payload_guard_cuts_images() -> None:
    files = StubFiles()
    parts = [part(i, image_file(files, f"f_{i}", raster_bytes=3_000_000)) for i in range(5)]
    parts.append(part(5, image_file(files, "f_5", raster_bytes=3_000_000, ocr=None)))
    view = await resolve(files, "opus-5.5", parts)  # 4 MB of base64 each; 18 MB binary budget: 4 fit
    assert [p.kind for p in view.parts] == ["native"] * 4 + ["ocr_text", "metadata_only"]  # ruling R3
    assert view.by_modality["image"] == "native" and [d.to for d in view.degradations] == ["ocr_text", "omitted"]
    assert ("f_4", "raster_1568", None) not in files.reads  # a cut image's raster is never read


async def test_read_failures_never_raise_and_json_is_canonical() -> None:
    files = StubFiles()
    files.fail.add(("f_i", "raster_1568", None))
    parts = [ContentPart(0, "json", None, json={"b": 1, "a": "é"}), part(1, image_file(files, "f_i"), "image-1")]
    view = await resolve(files, "haiku-5.5", parts)
    assert view.parts[0].blocks == (TextBlock('{"a":"é","b":1}'),)
    assert view.parts[1].kind == "metadata_only" and view.kind == "metadata_only"
    assert [d.reason for d in view.degradations] == ["not_ready"]


async def test_text_only_input_is_native() -> None:
    view = await resolve(StubFiles(), "deepseek-3.2", [text(0, "hello")])
    assert (view.kind, dict(view.by_modality), view.degradations) == ("native", {"text": "native"}, ())


@pytest.mark.parametrize("policy", ["derive_for_text_members", "skip_incapable_members"])
async def test_images_are_allocated_before_documents(policy: Policy) -> None:
    # regression (spec-07 §5 order): four 4.4 MB PDFs took the payload first and the image was cut to metadata_only
    # while by_modality still said image: native
    files = StubFiles()
    parts = [text(0, "Is the photo consistent with the contracts?")]
    parts.append(part(1, image_file(files, "f_img", raster_bytes=1_000_000), "image-1"))
    parts += [part(2 + i, pdf_file(files, f"f_p{i}", ["contract"], sanitized_bytes=4_400_000)) for i in range(4)]
    for alias in ("haiku-5.5", "opus-5.5"):
        view = await resolve(files, alias, parts, policy=policy)
        assert [(p.kind, len(p.blocks)) for p in view.parts[1:]] == [("native", 1)] * 3 + [("derived_text", 1)] * 2
        assert isinstance(view.parts[1].blocks[0], ImageBlock)
        # 18,000,000 - 1,333,336 (image) leaves room for two 5,866,668 B documents
        assert [(d.file_id, d.to) for d in view.degradations] == [("f_p2", "markdown"), ("f_p3", "markdown")]
        assert dict(view.by_modality) == {"text": "native", "image": "native", "document": "native"}


async def test_a_modality_whose_every_part_is_cut_is_metadata_only() -> None:
    # regression (IMPL-058): every image cut, yet by_modality said image: native and the member would run and count
    files = StubFiles()
    no_ocr = image_file(files, "f_a", ocr=None)  # a cut image with OCR shows it (ruling R3)
    parts = [text(0, "Compare."), part(1, no_ocr, "image-1"), part(2, pdf_file(files, "f_d", ["d"]))]
    budget = replace(VIEW_BUDGET_V1, max_images=0)
    view = await resolve(files, "opus-5.5", parts, budget=budget)
    assert [p.kind for p in view.parts] == ["native", "metadata_only", "native"]
    assert dict(view.by_modality) == {"text": "native", "image": "metadata_only", "document": "native"}
    assert view.kind == "metadata_only"
    skipped = await resolve(files, "opus-5.5", parts, budget=budget, policy="skip_incapable_members")
    assert skipped.kind == "skipped" and skipped.parts[1].kind == "skipped"
    # one image kept is enough: the cut one does not lower the modality (the budget is the same for every member)
    two = [*parts, part(3, image_file(files, "f_b", ocr=None), "image-2")]
    view = await resolve(files, "opus-5.5", two, budget=replace(VIEW_BUDGET_V1, max_images=1))
    assert [p.kind for p in view.parts] == ["native", "native", "native", "metadata_only"]
    assert view.by_modality["image"] == "native" and view.kind == "native"


@pytest.mark.parametrize("policy", ["derive_for_text_members", "skip_incapable_members"])
async def test_a_scanned_pdf_without_an_image_slot_is_not_seen(policy: Policy) -> None:
    # regression: 20 images took every slot; Luna saw nothing of the scanned PDF but by_modality said derived_text
    files = StubFiles()
    parts = [text(0, "Does the scan match the photos?")]
    parts += [part(1 + i, image_file(files, f"f_{i}"), f"image-{i + 1}") for i in range(20)]
    parts.append(part(21, pdf_file(files, "f_s", ["", ""], text_layer=(), ocr=["scan one", "scan two"]), "doc-1"))
    view = await resolve(files, "luna-6", parts, policy=policy)
    expected = "skipped" if policy == "skip_incapable_members" else "metadata_only"
    assert (view.parts[-1].kind, view.parts[-1].blocks) == (expected, ())
    assert view.by_modality["document"] == expected and view.kind == expected
    assert [(d.file_id, d.reason, d.to) for d in view.degradations] == [("f_s", "media_budget", "omitted")]


async def test_a_raster_over_the_image_limit_gets_the_ocr_row() -> None:
    # regression: a 3,900,000 B raster_1568 JPEG went out as a native ImageBlock (a Converse ValidationException)
    files = StubFiles()
    big = part(0, image_file(files, "f_big", raster_bytes=3_900_000, jpeg=True), "image-1")
    view = await resolve(files, "haiku-5.5", [big])
    assert (view.parts[0].kind, view.parts[0].blocks) == ("ocr_text", (TextBlock("INVOICE 4471"),))
    assert [(d.reason, d.to) for d in view.degradations] == [("size_limit", "ocr_text")]
    assert view.by_modality["image"] == "ocr_text" and ("f_big", "raster_1568", None) not in files.reads
    no_ocr = part(0, image_file(files, "f_none", raster_bytes=3_900_000, ocr=None), "image-1")
    view = await resolve(files, "haiku-5.5", [no_ocr])
    assert view.kind == "metadata_only"
    assert [(d.reason, d.to) for d in view.degradations] == [("size_limit", "omitted"), ("not_ready", "omitted")]


async def test_image_row_with_pending_page_images_reports_not_ready() -> None:
    # regression (spec-07 §5): a needed derivative that is not ready gives metadata_only with not_ready
    files = StubFiles()
    ref = pdf_file(files, "f_scan", ["", "", ""], ocr=["o1", "o2", "o3"], text_layer=())
    pending = tuple(replace(d, status="pending") if d.kind == "page_images" else d for d in ref.derivatives)
    parts = [part(0, replace(ref, status="partial", derivatives=pending), "doc-1")]
    view = await resolve(files, "sol-6.1", parts)
    assert view.kind == "metadata_only" and view.parts[0].blocks == ()
    assert [(d.reason, d.to) for d in view.degradations] == [("not_ready", "omitted")]
    text_view = await resolve(files, "deepseek-3.2", parts)  # the text row has the OCR
    assert text_view.kind == "ocr_text" and text_view.degradations == ()


@pytest.mark.parametrize("alias", ["glm-5.3", "sol-6.1"])
async def test_a_partial_cut_always_keeps_text(alias: str) -> None:
    # regression: a page starting with "\n" was cut at index 0: derived_text, truncated, with no blocks
    files = StubFiles()
    page = "\n" + "word " * 8000
    view = await resolve(files, alias, [part(0, pdf_file(files, "f_p", [page]), "doc-1")])
    (only,) = view.parts
    assert (only.kind, only.truncated, only.note) == ("derived_text", True, "page 1 of 1, partial")
    assert only.blocks == (TextBlock(page[:24_000]),)
    assert [d.to for d in view.degradations] == ["markdown(page 1 of 1, partial)"]


async def test_text_cut_to_nothing_is_metadata_only() -> None:
    files = StubFiles()
    parts = [part(0, pdf_file(files, "f_p", ["some text"]), "doc-1"), part(1, image_file(files, "f_i"), "image-1")]
    view = await resolve(files, "glm-5.3", parts, budget=replace(VIEW_BUDGET_V1, max_text_tokens=0))
    assert [(p.kind, p.blocks, p.truncated) for p in view.parts] == [("metadata_only", (), False)] * 2
    assert dict(view.by_modality) == {"document": "metadata_only", "image": "metadata_only"}
    assert [(d.reason, d.to) for d in view.degradations] == [("media_budget", "omitted")] * 2


async def test_page_images_of_uneven_size_stay_within_their_reservation() -> None:
    files = StubFiles()
    ref = pdf_file(files, "f_s", ["", "", "", ""], text_layer=(), page_image_bytes=100_000)  # 400,000 B declared
    files.data[("f_s", "page_images", 1)] = PNG + b"\x00" * 300_000  # real pages: 300 KB, then 100 KB each
    files.data[("f_s", "page_images", 4)] = PNG + b"\x00" * 3_750_000  # over the per-image limit
    view = await resolve(files, "luna-6", [part(0, ref, "doc-1")])
    (only,) = view.parts
    # reserved 4 x b64(100,000) = 533,344 B: page 1 (400,012) fits, pages 2-3 (133,336 each) no longer do
    assert (only.kind, [type(b).__name__ for b in only.blocks]) == ("derived_text", ["ImageBlock"])
    assert [(d.reason, d.to) for d in view.degradations] == [("size_limit", "markdown+page_images(1)")]


@pytest.mark.parametrize("policy", ["derive_for_text_members", "skip_incapable_members"])
async def test_a_budget_cut_image_falls_back_to_its_ocr_text(policy: Policy) -> None:
    # ruling R3 (spec-07 §5 step 1): a cut image gives its ready, non-empty ocr_text, else it is omitted
    files = StubFiles()
    parts = [
        part(0, image_file(files, "f_kept"), "image-1"),
        part(1, image_file(files, "f_ocr", ocr="RECEIPT 12"), "image-2"),
        part(2, image_file(files, "f_none", ocr=None), "image-3"),
        part(3, image_file(files, "f_empty", ocr=""), "image-4"),
        part(4, image_file(files, "f_blank", ocr=" \n "), "image-5"),  # declared non-empty, blank when read
        part(5, image_file(files, "f_pending", ocr_status="pending"), "image-6"),
        part(6, image_file(files, "f_fail"), "image-7"),
    ]
    files.fail.add(("f_fail", "ocr_text", None))
    view = await resolve(files, "opus-5.5", parts, budget=replace(VIEW_BUDGET_V1, max_images=1), policy=policy)
    assert [p.kind for p in view.parts] == ["native", "ocr_text"] + ["metadata_only"] * 5
    assert view.parts[1].blocks == (TextBlock("RECEIPT 12"),) and all(p.blocks == () for p in view.parts[2:])
    assert [(d.file_id, d.reason, d.to, d.part_index) for d in view.degradations] == [
        ("f_ocr", "media_budget", "ocr_text", 1),
        ("f_none", "media_budget", "omitted", 2),
        ("f_empty", "media_budget", "omitted", 3),
        ("f_blank", "media_budget", "omitted", 4),
        ("f_pending", "media_budget", "omitted", 5),
        ("f_fail", "media_budget", "omitted", 6),
        ("f_fail", "not_ready", "omitted", 6),
    ]
    # the cut keeps the pre-cut adequacy while the modality shows something (the budget is the same for everyone)
    assert view.by_modality["image"] == "native" and view.kind == "native"
    assert [r for r in files.reads if r[1] == "raster_1568"] == [("f_kept", "raster_1568", None)]
    assert sorted(r[0] for r in files.reads if r[1] == "ocr_text") == ["f_blank", "f_fail", "f_ocr"]


async def test_a_cut_images_ocr_shares_the_text_budget() -> None:
    files = StubFiles()
    ocr = "line of text\n" * 100  # 1,300 chars = 325 tokens
    parts = [part(0, image_file(files, "f_a"), "image-1"), part(1, image_file(files, "f_b", ocr=ocr), "image-2")]
    view = await resolve(files, "haiku-5.5", parts, budget=replace(VIEW_BUDGET_V1, max_images=1, max_text_tokens=100))
    cut = view.parts[1]
    assert (cut.kind, cut.truncated, cut.note) == ("ocr_text", True, "page 1 of 1, partial")
    assert cut.blocks == (TextBlock(ocr[:389]),)  # cut at the last "\n" within 400 chars
    assert [(d.reason, d.to) for d in view.degradations] == [
        ("media_budget", "ocr_text"),
        ("media_budget", "ocr_text(page 1 of 1, partial)"),
    ]
