"""CostMeter conformance (spec-02 §8, spec-11 §2, IMPL-019): Decimal pricing per 08:L508 against the snapshot's
catalog, rounding half-even to 1e-6, `estimated` for TBD components and estimated usage, and `record` -> one
`usage_line` outbox record keyed by the line id."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tests.fakes.config import FakeConfigRegistry, patched_example

from noema.config.loader import load_config
from noema.core.protocols.ops import UsageLine
from noema.core.values import Cost, UsageRecord

from .kits import CostKit
from .registry import kit_fixture

kit = kit_fixture("cost_meter")
PRICES = {
    "version": 1,
    "entries": [
        {
            "model": "us.openai.gpt-6.1-sol",
            "region": "us-east-1",
            "effective_from": "2026-01-01",
            "usd_per_mtok": {"input": "3", "output": "15", "cache_read": "0.3", "cache_write_5m": "3.75"},
        },
        {
            "model": "us.zai.glm-5.3",
            "region": "us-east-1",
            "effective_from": "2026-01-01",
            "usd_per_mtok": {"input": "1", "output": "TBD"},
        },
    ],
}


def priced() -> FakeConfigRegistry:
    body = patched_example({"platform": {"cost": {"prices_ref": "db:7"}}})
    return FakeConfigRegistry(load_config(body, env="test", prices=lambda v: PRICES if v == 7 else None))


async def test_price_follows_the_formula(kit: CostKit) -> None:
    meter, _ = kit.make(priced())
    usage = UsageRecord(
        input_tokens=1_000_000, output_tokens=200_000, cached_input_tokens=100_000, cache_write_input_tokens=50_000
    )
    cost = meter.price("sol-6.1", "us-east-1", usage)
    expected = (
        Decimal(850_000) * 3
        + Decimal(100_000) * Decimal("0.3")
        + Decimal(50_000) * Decimal("3.75")
        + Decimal(200_000) * 15
    ) / Decimal(1_000_000)
    assert cost == Cost(expected.quantize(Decimal("0.000001")), False)


async def test_rounding_is_half_even_per_call(kit: CostKit) -> None:
    meter, _ = kit.make(priced())
    cost = meter.price("sol-6.1", "us-east-1", UsageRecord(input_tokens=1, output_tokens=0))  # 3e-6 exactly
    assert cost.usd == Decimal("0.000003")
    tiny = meter.price("sol-6.1", "us-east-1", UsageRecord(input_tokens=0, output_tokens=0))
    assert tiny == Cost(Decimal("0.000000"), False)


async def test_tbd_component_marks_estimated(kit: CostKit) -> None:
    meter, _ = kit.make(priced())
    assert meter.price("glm-5.3", "us-east-1", UsageRecord(10, 0)).estimated is False
    assert meter.price("glm-5.3", "us-east-1", UsageRecord(10, 5)).estimated is True


async def test_missing_catalog_entry_costs_zero_estimated(kit: CostKit) -> None:
    meter, _ = kit.make(FakeConfigRegistry())  # the example config has no prices_ref
    assert meter.price("opus-5.5", "us-east-1", UsageRecord(100, 100)) == Cost(Decimal("0.000000"), True)


async def test_a_pinned_snapshot_prices_with_its_own_catalog(kit: CostKit) -> None:
    # IMPL-019 / ops-ledger.md item 4: a turn keeps its pinned catalog when another config becomes current
    registry = priced()
    pinned = registry.current()
    meter, _ = kit.make(registry)
    usage = UsageRecord(input_tokens=1_000_000, output_tokens=0)
    registry.activate(FakeConfigRegistry().current())  # the new current config has no prices_ref
    assert meter.price("sol-6.1", "us-east-1", usage, snapshot=pinned) == Cost(Decimal("3.000000"), False)
    assert meter.price("sol-6.1", "us-east-1", usage) == Cost(Decimal("0.000000"), True)


async def test_estimated_usage_marks_estimated(kit: CostKit) -> None:
    meter, _ = kit.make(priced())
    assert meter.price("sol-6.1", "us-east-1", UsageRecord(10, 10, tokens_estimated=True)).estimated


async def test_record_puts_one_usage_line(kit: CostKit) -> None:
    meter, records = kit.make(priced())
    line = UsageLine(
        id="0123456789abcdef0123456789",
        tenant_id="t_acme",
        project_id="p1",
        line="generation",
        model_id="us.openai.gpt-6.1-sol",
        region="us-east-1",
        usage=UsageRecord(10, 5),
        cost=Cost(Decimal("0.000105"), False),
        turn_id="turn_1",
        attempt_id="att_1",
        at=datetime(2026, 10, 10, 12, 0, tzinfo=UTC),
    )
    meter.record(line)
    [rec] = [r for r in records() if r.kind == "usage_line"]
    assert rec.id == line.id and rec.tenant_id == "t_acme"
    assert rec.payload["cost_usd"] == "0.000105" and rec.payload["line"] == "generation"
    assert rec.payload["at"] == "2026-10-10T12:00:00Z"
