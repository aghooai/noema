"""`Sampler` (spec-02 §9, spec-11 §3): uniform 0.02 + uncertainty 0.03, at most 2,000 offers per tenant and UTC day
(split over the pods), deterministic per decision, never blocking and never raising; the uniform slice has priority
over uncertainty offers and is spread over the day; a failed put uses no share."""

from __future__ import annotations

from datetime import timedelta

import pytest
from tests.fakes.clock import FakeClock
from tests.fakes.config import FakeConfigRegistry, patched_example, snapshot_from
from tests.fakes.outbox import FakeOutbox

from noema.core.protocols.decision import AnswerSummary, DecisionSnapshot
from noema.core.protocols.labeling import ContentRef
from noema.core.protocols.storage import OutboxRecord
from noema.core.values import JsonValue
from noema.labeling.sampler import Sampler, draw

pytestmark = pytest.mark.unit


def answer(name: str, band: str = "high", kind: str = "choice") -> AnswerSummary:
    return AnswerSummary(name, kind, "x", (("x", 0.9),), 0.9, 0.8, band, False, True)  # type: ignore[arg-type]


def snap(decision_id: str, *answers: AnswerSummary, hierarchical: tuple[str, ...] = ()) -> DecisionSnapshot:
    result: dict[str, JsonValue] = {
        "answers": [
            {"name": a.name, "type": a.type, **({"choice_method": "hierarchical"} if a.name in hierarchical else {})}
            for a in answers
        ]
    }
    return DecisionSnapshot(
        decision_id, "completed", True, frozenset(a.name for a in answers), {a.name: a for a in answers}, result
    )


def make(sample: dict[str, object] | None = None, *, pods: int = 1) -> tuple[Sampler, FakeOutbox, FakeClock]:
    clock = FakeClock()
    config = FakeConfigRegistry(snapshot_from(patched_example({"labeling": {"sample": sample}} if sample else {})))
    outbox = FakeOutbox()
    return Sampler(clock, config, outbox, expected_pods=pods), outbox, clock


def test_shipped_rates_sample_about_two_and_three_percent() -> None:
    sampler, outbox, _ = make({"uniform_rate": 0.02, "uncertainty_rate": 0.03, "max_per_day_per_tenant": 100_000})
    n = 20_000
    for i in range(n):
        sampler.offer("t_acme", snap(f"dec_{i:05d}", answer("intent", band="low")), ContentRef(f"dec_{i:05d}", None))
    reasons = [r.payload["sample_reason"] for r in outbox.records]
    uniform, uncertainty = reasons.count("uniform") / n, reasons.count("uncertainty") / n
    assert 0.017 < uniform < 0.023
    assert 0.025 < uncertainty < 0.035  # 0.03 of the non-uniform remainder


def test_offer_payload_and_determinism() -> None:
    sampler, outbox, _ = make({"uniform_rate": 1.0, "uncertainty_rate": 0.0})
    ref = ContentRef("dec_1", "turn_9")
    sampler.offer("t_acme", snap("dec_1", answer("intent"), answer("complexity", kind="score")), ref)
    record = outbox.records[0]
    assert (record.kind, record.id, record.tenant_id) == ("labeling_offer", "dec_1", "t_acme")
    assert record.payload == {
        "decision_id": "dec_1",
        "turn_id": "turn_9",
        "question_names": ["intent", "complexity"],
        "sample_reason": "uniform",
    }
    assert draw("dec_1", "uniform") == draw("dec_1", "uniform") != draw("dec_1", "uncertainty")


def test_unlabelable_answers_are_skipped() -> None:
    sampler, outbox, _ = make({"uniform_rate": 1.0, "uncertainty_rate": 0.0})
    s = snap(
        "dec_2",
        answer("a", kind="refusal"),
        answer("b", kind="unavailable"),
        answer("c"),
        answer("d"),
        hierarchical=("d",),
    )
    sampler.offer("t_acme", s, ContentRef("dec_2", None))
    assert outbox.records[0].payload["question_names"] == ["c"]
    sampler.offer("t_acme", snap("dec_3", answer("a", kind="refusal")), ContentRef("dec_3", None))
    assert len(outbox.records) == 1


def test_uncertainty_needs_a_low_band_answer() -> None:
    sampler, outbox, _ = make({"uniform_rate": 0.0, "uncertainty_rate": 1.0})
    sampler.offer("t_acme", snap("dec_lo", answer("q", band="low")), ContentRef("dec_lo", None))
    sampler.offer("t_acme", snap("dec_mid", answer("q", band="medium")), ContentRef("dec_mid", None))
    assert [r.id for r in outbox.records] == ["dec_lo"]


async def test_daily_cap_per_tenant_and_pod_share() -> None:
    sampler, outbox, clock = make({"uniform_rate": 1.0, "uncertainty_rate": 0.0, "max_per_day_per_tenant": 5}, pods=2)
    for i in range(10):
        sampler.offer("t_acme", snap(f"d{i}", answer("q")), ContentRef(f"d{i}", None))
        sampler.offer("t_beta", snap(f"e{i}", answer("q")), ContentRef(f"e{i}", None))
    assert [r.tenant_id for r in outbox.records].count("t_acme") == 3  # ceil(5 / 2) per pod
    assert [r.tenant_id for r in outbox.records].count("t_beta") == 3
    await clock.advance(timedelta(days=1).total_seconds())
    before = len(outbox.records)
    for i in range(10):  # a new day: the share is free again (thinned by yesterday's demand of 10 > 3)
        sampler.offer("t_acme", snap(f"next{i}", answer("q")), ContentRef(f"next{i}", None))
    assert 1 <= len(outbox.records) - before <= 3


def low_band_day(sampler: Sampler, outbox: FakeOutbox, day: int, n: int = 1000) -> list[tuple[int, str]]:
    """Offer n low-band decisions of one day in order; the (index, reason) of each offer made."""
    out: list[tuple[int, str]] = []
    for i in range(n):
        did = f"dec_{day}_{i:04d}"
        before = len(outbox.records)
        sampler.offer("t_acme", snap(did, answer("q", band="low")), ContentRef(did, None))
        if len(outbox.records) > before:
            out.append((i, str(outbox.records[-1].payload["sample_reason"])))
    return out


async def test_uncertainty_offers_cannot_crowd_out_the_uniform_slice() -> None:
    sampler, outbox, clock = make({"uniform_rate": 0.1, "uncertainty_rate": 1.0, "max_per_day_per_tenant": 10})
    first = [reason for _, reason in low_band_day(sampler, outbox, 1)]  # ~100 uniform and ~900 uncertainty draws
    assert len(first) == 10 and first.count("uncertainty") == 5  # no forecast yet: half the share is kept free
    await clock.advance(timedelta(days=1).total_seconds())
    second = [reason for _, reason in low_band_day(sampler, outbox, 2)]
    assert second and set(second) == {"uniform"}  # yesterday's uniform demand exceeds the share: no room left


async def test_the_uniform_slice_is_spread_over_the_day_once_demand_exceeds_the_share() -> None:
    sampler, outbox, clock = make({"uniform_rate": 0.5, "uncertainty_rate": 0.0, "max_per_day_per_tenant": 20})
    day1 = [i for i, _ in low_band_day(sampler, outbox, 1)]
    assert len(day1) == 20 and max(day1) < 100  # no forecast on the first day: the share fills early
    await clock.advance(timedelta(days=1).total_seconds())
    day2 = [i for i, _ in low_band_day(sampler, outbox, 2)]
    assert 10 <= len(day2) <= 20 and min(day2) < 250 and max(day2) > 750  # thinned by 20/~500: the whole day


class FlakyOutbox(FakeOutbox):
    def __init__(self) -> None:
        super().__init__()
        self.failures = 1

    def put(self, record: OutboxRecord) -> None:
        if self.failures:
            self.failures -= 1
            raise RuntimeError("outbox unavailable")
        super().put(record)


def test_a_failed_put_does_not_use_up_the_share() -> None:
    outbox = FlakyOutbox()
    config = FakeConfigRegistry(
        snapshot_from(patched_example({"labeling": {"sample": {"uniform_rate": 1.0, "max_per_day_per_tenant": 2}}}))
    )
    sampler = Sampler(FakeClock(), config, outbox)
    for i in range(4):
        sampler.offer("t_acme", snap(f"d{i}", answer("q")), ContentRef(f"d{i}", None))  # d0's put fails (logged)
    assert [r.id for r in outbox.records] == ["d1", "d2"]


def test_zero_rates_and_broken_snapshots_never_raise() -> None:
    sampler, outbox, _ = make({"uniform_rate": 0.0, "uncertainty_rate": 0.0})
    sampler.offer("t_acme", snap("dec", answer("q")), ContentRef("dec", None))
    assert outbox.records == []
    sampler2, _, _ = make({"uniform_rate": 1.0, "uncertainty_rate": 1.0})
    broken = DecisionSnapshot("dec_x", "completed", True, frozenset(), None, {})  # type: ignore[arg-type]
    sampler2.offer("t_acme", broken, ContentRef("dec_x", None))
