"""MED-09 plan vectors (spec-07 §4.5): slots and payload, current turn before the previous media turn, page-image
selection, text budgeting, labels, `media_budget` degradations, and the >20 images / >5 PDFs / 600-page cases."""

from __future__ import annotations

from dataclasses import replace

import pytest

from noema.core.protocols.media import CONVERSE_CAPS_V1, PlannedPart
from noema.core.values import Degradation
from noema.media.budget import MediaBudgetPlannerImpl

from .helpers import StubFiles, image_file, markdown_file, page_texts, part, pdf_file, text

pytestmark = pytest.mark.unit
PLANNER = MediaBudgetPlannerImpl()
CAPS = replace(CONVERSE_CAPS_V1, max_text_tokens=100_000)


def test_labels_and_origins() -> None:
    files = StubFiles()
    turn = [
        text(0, "compare"),
        part(1, image_file(files, "f_i1")),
        part(2, pdf_file(files, "f_p1", ["a"])),
        part(3, image_file(files, "f_i2")),
        part(4, markdown_file(files, "f_m1", "# notes")),
    ]
    history = [part(0, image_file(files, "f_h1")), part(1, pdf_file(files, "f_h2", ["b"]))]
    plan = PLANNER.plan(turn, history, CAPS)
    assert [(p.label, p.origin, p.kind, p.part_index) for p in plan.parts] == [
        ("image-1", "current", "image", 1),
        ("doc-1", "current", "pdf", 2),
        ("image-2", "current", "image", 3),
        ("doc-2", "current", "markdown", 4),
        ("image-3", "previous", "image", 0),
        ("doc-3", "previous", "pdf", 1),
    ]
    assert [p.file_id for p in plan.parts] == ["f_i1", "f_p1", "f_i2", "f_m1", "f_h1", "f_h2"]
    assert all(type(p) is PlannedPart and p.text_chars is None for p in plan.parts)  # the frozen type, no subclass
    assert plan.degradations == () and PLANNER.plan(turn, history, CAPS) == plan  # pure and deterministic


def test_more_than_20_images() -> None:
    files = StubFiles()
    parts = [part(i, image_file(files, f"f_{i}", ocr="text" if i != 21 else None)) for i in range(22)]
    plan = PLANNER.plan(parts, [], CAPS)
    assert [p.native for p in plan.parts] == [True] * 20 + [False, False]
    assert plan.degradations == (
        Degradation("image/png", "ocr_text", "media_budget", "f_20", 20),
        Degradation("image/png", "omitted", "media_budget", "f_21", 21),
    )


def test_more_than_5_pdfs_and_an_oversized_one() -> None:
    files = StubFiles()
    big = part(0, pdf_file(files, "f_big", ["x"], sanitized_bytes=4_500_001))
    parts = [big] + [part(i, pdf_file(files, f"f_{i}", ["x"])) for i in range(1, 7)]
    plan = PLANNER.plan(parts, [], CAPS)
    assert [p.native for p in plan.parts] == [False, True, True, True, True, True, False]
    assert plan.degradations == (Degradation("application/pdf", "markdown", "media_budget", "f_6", 6),)  # big: no slot


def test_payload_budget_cuts_images_and_documents() -> None:
    files = StubFiles()
    parts = [
        part(0, image_file(files, "f_a", raster_bytes=3_000_000)),
        part(1, image_file(files, "f_b", raster_bytes=3_000_000)),
        part(2, pdf_file(files, "f_c", ["x"], sanitized_bytes=3_000_000, page_images=False)),
    ]
    caps = replace(
        CAPS, max_payload_bytes=10_000_000, text_reserve_bytes=1_000_000
    )  # 9 MB binary; 4 MB per base64 item
    plan = PLANNER.plan(parts, [], caps)
    assert [p.native for p in plan.parts] == [True, True, False]
    assert [(d.file_id, d.to) for d in plan.degradations] == [("f_c", "markdown")]


def test_not_ready_and_oversized_images_take_no_slot() -> None:
    files = StubFiles()
    pending = part(0, image_file(files, "f_p", raster_status="pending"))
    huge = part(1, image_file(files, "f_h", raster_bytes=3_750_001))
    ok = part(2, image_file(files, "f_ok"))
    plan = PLANNER.plan([pending, huge, ok], [], replace(CAPS, max_images=1))
    assert [p.native for p in plan.parts] == [False, False, True] and plan.degradations == ()


def test_page_image_selection_terms_then_scanned() -> None:
    files = StubFiles()
    md = ["cover", "intro", "", "invoice here", "", "", "invoice total", "", "total invoice", "end"]
    doc = part(1, pdf_file(files, "f_doc", md), texts=page_texts(md))  # pages 3, 5, 6, 8 have no text layer
    turn = [text(0, "What is the invoice total?"), doc]
    plan = PLANNER.plan(turn, [], replace(CAPS, max_images=6))
    assert plan.parts[0].page_images == (1, 2, 3, 4, 7, 9)  # 1-2, then 7, 9 (two terms), 4 (one), then scanned 3
    assert plan.degradations == ()


def test_page_image_slots_are_shared_between_pdfs() -> None:
    files = StubFiles()
    images = [part(i, image_file(files, f"f_i{i}")) for i in range(2)]
    pdfs = [part(2 + j, pdf_file(files, f"f_p{j}", ["t"] * 10)) for j in range(3)]
    plan = PLANNER.plan(images + pdfs, [], CAPS)
    assert [len(p.page_images) for p in plan.parts[2:]] == [6, 6, 6]  # floor(18 / 3) each
    plan = PLANNER.plan(images + pdfs[:1] + [part(9, pdf_file(files, "f_short", ["t"]))], [], CAPS)
    assert [len(p.page_images) for p in plan.parts[2:]] == [9, 1]  # floor(18/2) = 9; the 1-page PDF takes 1


def test_page_images_stop_at_the_payload() -> None:
    files = StubFiles()
    doc = part(0, pdf_file(files, "f_doc", ["t"] * 8, sanitized_bytes=1000, page_image_bytes=1_500_000))
    caps = replace(CAPS, max_payload_bytes=8_000_000, text_reserve_bytes=0)  # 2,000,000 B per page in base64
    assert PLANNER.plan([doc], [], caps).parts[0].page_images == (1, 2, 3)


def test_text_budget_cuts_and_degradations() -> None:
    files = StubFiles()
    md = ["p" * 400] * 5  # 100 tokens per page, 2 more per page boundary
    a = part(0, pdf_file(files, "f_a", md), texts=md)
    b = part(1, markdown_file(files, "f_b", "m" * 4000), texts=["m" * 4000])  # 1,000 tokens, one page
    plan = PLANNER.plan([a, b], [], replace(CAPS, max_text_tokens=700))
    pa, pb = plan.parts
    assert (pa.text_pages, pa.text_cut) == (3, "pages 1-3 of 5")  # share 350: 3 pages = 1,204 chars = 301 tokens
    assert (pb.text_pages, pb.text_cut) == (0, "page 1 of 1, partial")  # share 700 - 301 = 399 < 1,000
    assert (pa.text_chars, pb.text_chars) == (None, 399 * 4)
    assert [d.to for d in plan.degradations] == ["markdown(pages 1-3 of 5)", "markdown(page 1 of 1, partial)"]
    assert all(d.reason == "media_budget" and d.attempt_id is None and d.model is None for d in plan.degradations)


def test_zero_text_allowance_keeps_no_text_measured_or_not() -> None:
    files = StubFiles()
    a = part(0, pdf_file(files, "f_a", ["text"]), texts=["text"])
    b = part(1, pdf_file(files, "f_b", ["text", "more"]))  # page_texts not read: unmeasured
    plan = PLANNER.plan([a, b], [], CONVERSE_CAPS_V1)  # max_text_tokens = 0
    assert [(p.text_pages, p.text_cut) for p in plan.parts] == [
        (0, "page 1 of 1, partial"),
        (0, "page 1 of 2, partial"),
    ]
    assert [(d.file_id, d.to) for d in plan.degradations] == [("f_a", "omitted"), ("f_b", "omitted")]


def test_unmeasured_text_shares_the_allowance() -> None:
    # regression: page_texts=None was planned whole outside the allowance (a 600-page PDF sent ~540,000 tokens)
    files = StubFiles()
    small = part(0, markdown_file(files, "f_s", "s" * 396))  # manifest 396 B: an upper bound of 100 tokens
    big = part(1, pdf_file(files, "f_b", ["lorem ipsum " * 300] * 600))  # 2.16 MB of Markdown
    pending = pdf_file(files, "f_p", ["x"])
    pending = replace(
        pending,
        derivatives=tuple(replace(d, status="pending") if d.kind == "markdown" else d for d in pending.derivatives),
    )
    plan = PLANNER.plan([small, big, part(2, pending)], [], replace(CAPS, max_text_tokens=10_000))
    ps, pb, pp = plan.parts
    assert (ps.text_pages, ps.text_cut) == (1, None)  # fits its share of 3,333: whole, uses 100
    # big: share floor(9,900 / 2) = 4,950 cannot hold the estimate, so page 1 is hard-cut at the share
    assert (pb.text_pages, pb.text_cut) == (0, "page 1 of 600, partial")
    assert pb.text_chars == 4_950 * 4
    assert (pp.text_pages, pp.text_cut) == (1, None)  # no ready Markdown: nothing can be sent, nothing is cut
    assert [(d.file_id, d.to) for d in plan.degradations] == [("f_b", "markdown(page 1 of 600, partial)")]


def test_given_labels_are_kept_for_both_turns() -> None:
    # regression: previous-turn labels were renumbered, so preflight's history labels (which also number the
    # previous files whose lookup failed) collided with the plan's
    files = StubFiles()
    turn = [part(0, image_file(files, "f_cur"), "image-1")]
    history = [
        part(1, image_file(files, "f_b"), "image-3"),  # image-2 belongs to a dropped file of that turn
        part(2, pdf_file(files, "f_d", ["d"]), "doc-1"),  # clashes with nothing: kept
        part(3, image_file(files, "f_e"), "image-1"),  # clashes with the current turn: renumbered
        part(4, image_file(files, "f_f"), "report.png"),  # a filename is never a label
    ]
    plan = PLANNER.plan(turn, history, CAPS)
    assert [(p.label, p.origin) for p in plan.parts] == [
        ("image-1", "current"),
        ("image-3", "previous"),
        ("doc-1", "previous"),
        ("image-2", "previous"),
        ("image-4", "previous"),
    ]


def test_history_parts_come_after_the_current_turn() -> None:
    files = StubFiles()
    turn = [part(i, image_file(files, f"f_c{i}")) for i in range(19)]
    history = [part(i, image_file(files, f"f_h{i}")) for i in range(2)]
    plan = PLANNER.plan(turn, history, CAPS)
    assert [p.native for p in plan.parts[19:]] == [True, False]
    assert plan.degradations == (Degradation("image/png", "ocr_text", "media_budget", "f_h1", None),)
