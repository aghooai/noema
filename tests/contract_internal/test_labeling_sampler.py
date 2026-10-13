"""LabelingSampler conformance (spec-02 §9, spec-11 §3): `offer` never raises, samples by `labeling.sample`
rates (deterministic per decision), respects the per-tenant daily cap, and puts `labeling_offer` records."""

from __future__ import annotations

from tests.fakes.config import FakeConfigRegistry, patched_example, snapshot_from

from noema.core.protocols.decision import AnswerSummary, DecisionSnapshot
from noema.core.protocols.labeling import ContentRef

from .kits import SamplerKit
from .registry import kit_fixture

kit = kit_fixture("labeling_sampler")


def registry(uniform: float, uncertainty: float = 0.0, cap: int | None = None) -> FakeConfigRegistry:
    sample: dict[str, object] = {"uniform_rate": uniform, "uncertainty_rate": uncertainty}
    if cap is not None:
        sample["max_per_day_per_tenant"] = cap
    return FakeConfigRegistry(snapshot_from(patched_example({"labeling": {"sample": sample}})))


def snap(decision_id: str, band: str = "high", kind: str = "choice") -> DecisionSnapshot:
    answer = AnswerSummary("intent", kind, "coding", (("coding", 0.9),), 0.9, 0.8, band, False, True)  # type: ignore[arg-type]
    return DecisionSnapshot(decision_id, "completed", True, frozenset({"intent"}), {"intent": answer}, {})


async def test_rate_one_samples_every_decision(kit: SamplerKit) -> None:
    sampler, records = kit.make(registry(1.0))
    for i in range(3):
        sampler.offer("t_acme", snap(f"dec_{i}"), ContentRef(f"dec_{i}", None))
    offers = [r for r in records() if r.kind == "labeling_offer"]
    assert [r.id for r in offers] == ["dec_0", "dec_1", "dec_2"]
    assert offers[0].payload["question_names"] == ["intent"] and offers[0].payload["sample_reason"] == "uniform"


async def test_rate_zero_samples_nothing_and_caps_hold(kit: SamplerKit) -> None:
    sampler, records = kit.make(registry(0.0))
    sampler.offer("t_acme", snap("dec_a"), ContentRef("dec_a", "turn_1"))
    assert records() == []
    capped, capped_records = kit.make(registry(1.0, cap=2))
    for i in range(5):
        capped.offer("t_acme", snap(f"dec_{i}"), ContentRef(f"dec_{i}", None))
    assert len(capped_records()) == 2


async def test_uncertainty_sampling_reads_the_band(kit: SamplerKit) -> None:
    sampler, records = kit.make(registry(0.0, uncertainty=1.0))
    sampler.offer("t_acme", snap("dec_low", band="low"), ContentRef("dec_low", None))
    sampler.offer("t_acme", snap("dec_high", band="high"), ContentRef("dec_high", None))
    assert [(r.id, r.payload["sample_reason"]) for r in records()] == [("dec_low", "uncertainty")]


async def test_offer_never_raises(kit: SamplerKit) -> None:
    sampler, _ = kit.make(registry(1.0))
    broken = DecisionSnapshot("dec_x", "completed", True, frozenset(), None, {})  # type: ignore[arg-type]
    sampler.offer("t_acme", broken, ContentRef("dec_x", None))
