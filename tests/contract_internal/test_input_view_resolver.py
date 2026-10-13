"""InputViewResolver conformance (spec-02 §6, spec-06 §4): native views for capable models, derived views or
`skipped` per policy, `metadata_only` + `not_ready` for unusable files (never raises), budget degradations."""

from __future__ import annotations

from collections.abc import AsyncIterator

from noema.core.protocols.media import VIEW_BUDGET_V1, ContentPart, DecisionViewBudget, FileRef
from noema.core.values import ImageBlock, TextBlock

from .kits import MediaKit, principal
from .registry import kit_fixture

kit = kit_fixture("input_view_resolver")
PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 64


async def chunks(data: bytes) -> AsyncIterator[bytes]:
    yield data


async def image(kit: MediaKit) -> FileRef:
    return await kit.files.upload(
        principal(), chunks(PNG), declared_type="image/png", filename="i.png", session_id=None
    )


def member(kit: MediaKit, alias: str):  # type: ignore[no-untyped-def]
    profile = kit.snapshot.decision_profile("router-default")
    return next(m for m in profile.members if m.model == alias)


async def test_text_parts_are_native(kit: MediaKit) -> None:
    m = member(kit, "haiku-5.5")
    view = await kit.views.resolve(
        m,
        [ContentPart(0, "text", None, text="hello")],
        policy="derive_for_text_members",
        budget=VIEW_BUDGET_V1,
        model=kit.snapshot.model("haiku-5.5"),
        principal=principal(),
    )
    assert view.kind == "native" and view.parts[0].blocks == (TextBlock("hello"),)


async def test_image_views_by_capability_and_policy(kit: MediaKit) -> None:
    ref = await image(kit)
    part = ContentPart(0, "image", "image-1", file=ref)
    haiku = await kit.views.resolve(
        member(kit, "haiku-5.5"),
        [part],
        policy="derive_for_text_members",
        budget=VIEW_BUDGET_V1,
        model=kit.snapshot.model("haiku-5.5"),
        principal=principal(),
    )
    assert haiku.kind == "native" and isinstance(haiku.parts[0].blocks[0], ImageBlock)
    deepseek = kit.snapshot.model("deepseek-3.2")
    derived = await kit.views.resolve(
        member(kit, "deepseek-3.2"),
        [part],
        policy="derive_for_text_members",
        budget=VIEW_BUDGET_V1,
        model=deepseek,
        principal=principal(),
    )
    assert derived.kind in ("ocr_text", "thumbnail") and derived.by_modality["image"] == derived.kind
    skipped = await kit.views.resolve(
        member(kit, "deepseek-3.2"),
        [part],
        policy="skip_incapable_members",
        budget=VIEW_BUDGET_V1,
        model=deepseek,
        principal=principal(),
    )
    assert skipped.kind == "skipped"


async def test_unready_file_is_metadata_only(kit: MediaKit) -> None:
    ref = await image(kit)
    from dataclasses import replace

    pending = replace(ref, status="processing")
    view = await kit.views.resolve(
        member(kit, "haiku-5.5"),
        [ContentPart(0, "image", "image-1", file=pending)],
        policy="derive_for_text_members",
        budget=VIEW_BUDGET_V1,
        model=kit.snapshot.model("haiku-5.5"),
        principal=principal(),
    )
    assert view.kind == "metadata_only" and [d.reason for d in view.degradations] == ["not_ready"]


async def test_budget_caps_images(kit: MediaKit) -> None:
    ref = await image(kit)
    parts = [ContentPart(i, "image", f"image-{i + 1}", file=ref) for i in range(3)]
    budget = DecisionViewBudget(max_text_tokens=6000, max_images=2, max_documents=5, image_long_edge=1568)
    view = await kit.views.resolve(
        member(kit, "haiku-5.5"),
        parts,
        policy="derive_for_text_members",
        budget=budget,
        model=kit.snapshot.model("haiku-5.5"),
        principal=principal(),
    )
    # spec-07 §5 step 1 (ruling R3): a cut image falls back to its ready, non-empty ocr_text
    assert [p.kind for p in view.parts] == ["native", "native", "ocr_text"]
    assert isinstance(view.parts[2].blocks[0], TextBlock) and view.parts[2].blocks[0].text.strip()
    assert [(d.reason, d.to, d.part_index) for d in view.degradations] == [("media_budget", "ocr_text", 2)]
    # adequacy ignores the cut while other images show (spec-07 §5): the member is not metadata_only
    assert view.by_modality["image"] == "native" and view.kind == "native"
