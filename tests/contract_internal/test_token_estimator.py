"""TokenEstimator conformance (spec-11 §2, IMPL-029c): ceil(chars / 4.0) per system string and text block,
min(1600, ceil(w·h/750)) per image, 1500 per document page."""

from __future__ import annotations

from noema.core.values import DocumentBlock, ImageBlock, TextBlock

from .kits import EstimatorKit
from .registry import kit_fixture

kit = kit_fixture("token_estimator")


async def test_text_rounds_up_per_item(kit: EstimatorKit) -> None:
    assert kit.estimator.estimate("sol-6.1", ["abcde"], [TextBlock("abc"), TextBlock("")]) == 2 + 1 + 0
    assert kit.estimator.estimate("sol-6.1", [], []) == 0


async def test_images_and_documents(kit: EstimatorKit) -> None:
    small = ImageBlock("png", b"x", 100, 75)  # ceil(7500/750) = 10
    big = ImageBlock("jpeg", b"x", 4000, 3000)  # capped at 1600
    doc = DocumentBlock("pdf", "doc-1", b"%PDF", pages=3)
    assert kit.estimator.estimate("opus-5.5", [], [small, big, doc]) == 10 + 1600 + 4500
