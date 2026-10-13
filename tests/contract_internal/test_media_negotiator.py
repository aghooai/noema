"""MediaNegotiator + MediaBudgetPlanner conformance (spec-02 §6, spec-07 §4): per-model blocks with
`model_capability` degradations, a document travels with a text block, and the planner's caps (`media_budget`)."""

from __future__ import annotations

from collections.abc import AsyncIterator

from noema.core.protocols.media import CONVERSE_CAPS_V1, ContentPart, FileRef, MediaCaps
from noema.core.values import DocumentBlock, ImageBlock, TextBlock

from .kits import MediaKit, principal
from .registry import kit_fixture

kit = kit_fixture("media_negotiator")
budget_kit = kit_fixture("media_budget")
PNG = b"\x89PNG\r\n\x1a\n" + b"\x02" * 64
PDF = b"%PDF-1.7\n" + b"\x03" * 64


async def chunks(data: bytes) -> AsyncIterator[bytes]:
    yield data


async def upload(kit: MediaKit, data: bytes, declared: str, name: str) -> FileRef:
    return await kit.files.upload(principal(), chunks(data), declared_type=declared, filename=name, session_id=None)


async def test_negotiation_per_model(kit: MediaKit) -> None:
    img = await upload(kit, PNG, "image/png", "i.png")
    pdf = await upload(kit, PDF, "application/pdf", "d.pdf")
    parts = [
        ContentPart(0, "text", None, text="Compare these."),
        ContentPart(1, "image", "image-1", file=img),
        ContentPart(2, "file", "doc-1", file=pdf),
    ]
    plan = kit.media_budget.plan(parts, [], CONVERSE_CAPS_V1)
    opus = kit.snapshot.model("opus-5.5")
    blocks, degradations = await kit.negotiator.negotiate("opus-5.5", opus, parts, plan, principal=principal())
    assert any(isinstance(b, ImageBlock) for b in blocks) and any(isinstance(b, DocumentBlock) for b in blocks)
    assert any(isinstance(b, TextBlock) for b in blocks) and degradations == ()
    sol = kit.snapshot.model("sol-6.1")  # images yes, PDFs no
    blocks, degradations = await kit.negotiator.negotiate("sol-6.1", sol, parts, plan, principal=principal())
    assert not any(isinstance(b, DocumentBlock) for b in blocks)
    assert [d.reason for d in degradations] == ["model_capability"] and degradations[0].file_id == pdf.file_id


async def test_text_parts_get_no_blocks(kit: MediaKit) -> None:
    # the prompt builder escapes and spotlights user text itself (spec-09 §5); the negotiator renders media only
    img = await upload(kit, PNG, "image/png", "i.png")
    parts = [ContentPart(0, "text", None, text="Compare these."), ContentPart(1, "image", "image-1", file=img)]
    plan = kit.media_budget.plan(parts, [], CONVERSE_CAPS_V1)
    opus = kit.snapshot.model("opus-5.5")
    blocks, _ = await kit.negotiator.negotiate("opus-5.5", opus, parts[:1], plan, principal=principal())
    assert blocks == ()
    blocks, _ = await kit.negotiator.negotiate("opus-5.5", opus, parts, plan, principal=principal())
    assert not any(isinstance(b, TextBlock) and "Compare these." in b.text for b in blocks)
    assert any(isinstance(b, ImageBlock) for b in blocks)


async def test_planned_parts_carry_file_ids_and_labels(budget_kit: MediaKit) -> None:
    # media-negotiation.md item 1: a history part whose index repeats a current one is found by its file id
    img = await upload(budget_kit, PNG, "image/png", "i.png")
    pdf = await upload(budget_kit, PDF, "application/pdf", "d.pdf")
    current = [ContentPart(0, "image", None, file=img), ContentPart(1, "file", "report.pdf", file=pdf)]
    previous = [ContentPart(0, "file", None, file=pdf)]
    plan = budget_kit.media_budget.plan(current, previous, CONVERSE_CAPS_V1)
    got = [(p.origin, p.part_index, p.file_id, p.label) for p in plan.parts]
    assert got == [
        ("current", 0, img.file_id, "image-1"),
        ("current", 1, pdf.file_id, "doc-1"),  # a filename never becomes a label (spec-09 §5)
        ("previous", 0, pdf.file_id, "doc-2"),
    ]


async def test_planner_caps(budget_kit: MediaKit) -> None:
    img = await upload(budget_kit, PNG, "image/png", "i.png")
    parts = [ContentPart(i, "image", f"image-{i + 1}", file=img) for i in range(3)]
    caps = MediaCaps(max_images=2)
    plan = budget_kit.media_budget.plan(parts, [], caps)
    assert [p.native for p in plan.parts] == [True, True, False]
    assert [d.reason for d in plan.degradations] == ["media_budget"]
    assert budget_kit.media_budget.plan(parts, [], caps) == plan  # pure
