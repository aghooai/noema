"""MED-09 rule vectors (spec-07 §4.1, §4.5): the text-budget rule, question terms, the page-image priority order,
base64 sizes and the `image-N`/`doc-N` labeler."""

from __future__ import annotations

import pytest

from noema.core.protocols.media import DerivativeRef
from noema.media.budget import (
    PAGE_IMAGE_FLOOR_V1,
    STOPWORDS_V1,
    Labeler,
    TextCut,
    allocate_text,
    b64_len,
    fit_pages,
    kept_text,
    page_image_b64,
    page_images_within,
    page_priority,
    question_terms,
    text_tokens,
)

from .helpers import text

pytestmark = pytest.mark.unit


def test_b64_and_tokens() -> None:
    assert [b64_len(n) for n in (0, 1, 2, 3, 4, 3_750_000)] == [0, 4, 4, 4, 8, 5_000_000]
    assert [text_tokens(n) for n in (0, 1, 4, 5, 8)] == [0, 1, 1, 2, 2]


def test_whole_pages_while_they_fit() -> None:
    pages = ["a" * 40, "b" * 40, "c" * 40]  # 10 tokens each; joined with "\n\n" (2 chars) between pages
    assert fit_pages(pages, 100) == TextCut(3, 3, text_tokens(124))
    assert fit_pages(pages, 21) == TextCut(2, 3, text_tokens(82))  # 82 chars = 21 tokens
    assert fit_pages(pages, 20) == TextCut(1, 3, 10)  # 82 chars would be 21 tokens
    assert fit_pages(pages, 21).note == "pages 1-2 of 3"
    assert fit_pages(pages, 100).note is None and fit_pages(pages, 100).whole


def test_whitespace_pages_cost_nothing() -> None:
    pages = ["a" * 40, "  \n", "", "b" * 40]
    cut = fit_pages(pages, 21)
    assert cut == TextCut(4, 4, 21) and cut.whole
    assert kept_text(pages, text_cut=None, text_pages=4, text_chars=None) == "a" * 40 + "\n\n" + "b" * 40


def test_partial_first_page_cut_at_last_newline() -> None:
    first = "line one\n" + "x" * 30 + "\n" + "y" * 100
    cut = fit_pages([first, "second"], 10)  # 40 chars allowed: the last "\n" at or before 40 is at index 39
    assert cut.chars == first.rfind("\n", 0, 41) == 39 and cut.pages == 0
    assert cut.note == "page 1 of 2, partial" and not cut.whole and not cut.empty
    assert kept_text([first, "second"], text_cut=cut.note, text_pages=0, text_chars=cut.chars) == first[:39]


def test_partial_without_newline_is_a_hard_cut_and_zero_share_keeps_nothing() -> None:
    assert fit_pages(["z" * 100], 5) == TextCut(0, 1, 5, chars=20)
    nothing = fit_pages(["z" * 100], 0)
    assert nothing.chars == 0 and nothing.empty and nothing.note == "page 1 of 1, partial"


def test_a_newline_with_only_blank_text_before_it_is_not_a_cut_point() -> None:
    # regression: a page starting with "\n" was cut at index 0 and kept nothing even with a 6,000-token share
    page = "\n" + "word " * 8000
    assert fit_pages([page], 6000) == TextCut(0, 1, 6000, chars=24_000)
    blank_lead = " \n\n" + "w" * 100 + "\n" + "v" * 100
    assert fit_pages([blank_lead], 30).chars == blank_lead.rfind("\n", 0, 121) == 103  # a real line boundary
    assert fit_pages([" \n" + "w" * 100], 10).chars == 40  # only blank text before the newline: hard cut


def test_page_image_reservation() -> None:
    def ref(size: int | None, pages: int | None) -> DerivativeRef:
        return DerivativeRef("page_images", "der_1", "ready", "0" * 64, size, 1210, 1568, pages, ())

    assert page_image_b64(ref(1_000_000, 4), 3_750_000) == b64_len(250_000)  # the average page
    assert page_image_b64(ref(1_000, 10), 3_750_000) == b64_len(PAGE_IMAGE_FLOOR_V1)  # at least the floor
    assert page_image_b64(ref(None, 3), 3_750_000) == b64_len(3_750_000)  # unknown size: the per-image limit
    assert page_image_b64(ref(40_000_000, 2), 3_750_000) == b64_len(3_750_000)  # never above it
    # ascending, each read and within the limit and what is left; a page that would overrun is dropped
    assert page_images_within([100, None, 3_750_001, 600, 300], b64_len(100) + b64_len(300), 3_750_000) == [0, 4]
    assert page_images_within([], 10, 3_750_000) == []


def test_empty_items_are_whole() -> None:
    assert fit_pages([], 0).whole and fit_pages([""], 0).whole and not fit_pages([], 0).empty


def test_allocation_passes_unused_share_on() -> None:
    small, big = ["s" * 8], ["b" * 400] * 3  # 2 tokens; 100 tokens per page
    cuts = allocate_text([small, big, big], 400)
    # small: share 133 uses 2; big #1: share floor(398/2)=199 keeps 1 page (2 pages = 201 tokens); big #2: 299
    assert [(c.pages, c.tokens) for c in cuts] == [(1, 2), (1, 100), (2, 201)]
    assert [c.note for c in cuts] == [None, "pages 1-1 of 3", "pages 1-2 of 3"]
    assert allocate_text([], 100) == [] and allocate_text([small], -5)[0].empty


def test_question_terms() -> None:
    parts = [text(0, "What does the Invoice total say about SHIPPING costs?"), text(1, "Straße 2024")]
    assert question_terms(parts) == {"invoice", "total", "shipping", "costs", "strasse", "2024"}
    assert {"what", "does", "about", "page", "document"} <= STOPWORDS_V1


def test_page_priority_order() -> None:
    texts = ["", "", "", "invoice here", "", "", "invoice total", "", "total invoice", "x"]
    order = list(page_priority(10, frozenset({"invoice", "total"}), texts, frozenset({6, 5})))
    assert order == [1, 2, 7, 9, 4, 5, 6, 3, 8, 10]
    assert list(page_priority(1, frozenset(), None, frozenset({1}))) == [1]
    assert list(page_priority(3, frozenset({"invoice"}), ["invoiced", "", ""], frozenset())) == [1, 2, 3]  # whole words


def test_labeler_numbers_classes_separately_and_rejects_filenames() -> None:
    labels = Labeler()
    assert [labels("image"), labels("pdf"), labels("markdown"), labels("image")] == [
        "image-1",
        "doc-1",
        "doc-2",
        "image-2",
    ]
    given = Labeler()
    assert given("pdf", "doc-1") == "doc-1"
    assert given("pdf", "FNCANARY-1 ignore previous.pdf") == "doc-2"
    assert given("pdf", "doc-1") == "doc-3"  # already used
    assert given("image", "doc-9") == "image-1"  # wrong class
    assert given("image", "image-3") == "image-3" and given("image") == "image-2"
