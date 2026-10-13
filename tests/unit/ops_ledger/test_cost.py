"""T-COST-1 (spec-11 §8): price lookup precedence (price_ref, id, region, effective_from), the 08:L508 formula
against hand-computed Decimal vectors, half-even rounding per call, TBD/missing -> estimated, an alias missing from
the current config -> TBD (never raises); a pinned snapshot prices with its own catalog (IMPL-019); `record`."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from tests.fakes.clock import FakeClock
from tests.fakes.config import FakeConfigRegistry, patched_example
from tests.fakes.outbox import FakeOutbox
from tests.fakes.telemetry import FakeTelemetry

from noema.config.loader import load_config
from noema.core.protocols.ops import UsageLine
from noema.core.values import Cost, UsageRecord
from noema.ops.cost import LedgerCostMeter

pytestmark = pytest.mark.unit

OPUS = "us.anthropic.claude-opus-5-5"
REF = "anthropic.claude-opus-5-5.list-price"


def entry(model: str, region: str, day: str, **rates: str) -> dict[str, Any]:
    return {"model": model, "region": region, "effective_from": day, "usd_per_mtok": rates}


CATALOG = {
    "version": 1,
    "entries": [
        entry(OPUS, "us-east-1", "2026-01-01", input="9", output="99"),  # shadowed by the price_ref entry
        entry(REF, "us-east-1", "2026-01-01", input="3", output="15"),
        entry(REF, "us-east-1", "2026-10-01", input="4", output="16"),
        entry(REF, "us-east-1", "2026-12-01", input="5", output="17"),
        entry(REF, "us-west-2", "2026-01-01", input="6", output="18"),
        entry(
            "us.openai.gpt-6.1-sol",
            "us-east-1",
            "2026-01-01",
            input="2.5",
            output="12.5",
            cache_read="0.25",
            cache_write_5m="3.125",
            cache_write_1h="5",
        ),
        entry("deepseek.v3.2", "us-east-1", "2026-01-01", input="0.5", output="TBD"),
    ],
}


def registry(*, price_ref: bool = True) -> FakeConfigRegistry:
    patch: dict[str, Any] = {"platform": {"cost": {"prices_ref": "db:3"}}}
    if price_ref:
        patch["models"] = {"opus-5.5": {"price_ref": REF}}
    body = patched_example(patch)
    return FakeConfigRegistry(load_config(body, env="test", prices=lambda v: CATALOG if v == 3 else None))


def meter(wall: datetime = datetime(2026, 10, 10, 12, tzinfo=UTC), **kw: Any) -> tuple[LedgerCostMeter, FakeOutbox]:
    outbox = FakeOutbox()
    telemetry = kw.pop("telemetry", FakeTelemetry())
    return LedgerCostMeter(FakeClock(wall=wall), registry(**kw), outbox, telemetry.instruments), outbox


MTOK = UsageRecord(input_tokens=1_000_000, output_tokens=1_000_000)


def test_price_ref_wins_over_the_bedrock_id() -> None:
    m, _ = meter()
    assert m.price("opus-5.5", "us-east-1", MTOK) == Cost(Decimal("20.000000"), False)  # 4 + 16 (2026-10-01 row)
    m, _ = meter(price_ref=False)
    assert m.price("opus-5.5", "us-east-1", MTOK) == Cost(Decimal("108.000000"), False)  # the id row: 9 + 99


@pytest.mark.parametrize(
    ("wall", "usd"),
    [
        (datetime(2026, 9, 30, 23, 59, tzinfo=UTC), "18.000000"),
        (datetime(2026, 10, 1, 0, 0, tzinfo=UTC), "20.000000"),
        (datetime(2026, 11, 30, 23, 59, tzinfo=UTC), "20.000000"),
        (datetime(2026, 12, 1, 0, 0, tzinfo=UTC), "22.000000"),
    ],
)
def test_newest_effective_from_not_after_now(wall: datetime, usd: str) -> None:
    m, _ = meter(wall)
    assert m.price("opus-5.5", "us-east-1", MTOK) == Cost(Decimal(usd), False)


def test_region_is_part_of_the_key() -> None:
    m, _ = meter()
    assert m.price("opus-5.5", "us-west-2", MTOK) == Cost(Decimal("24.000000"), False)
    assert m.price("opus-5.5", "eu-west-1", MTOK) == Cost(Decimal("0.000000"), True)


def test_before_the_first_effective_date_is_unpriced() -> None:
    m, _ = meter(datetime(2025, 12, 31, 23, 59, tzinfo=UTC))
    assert m.price("opus-5.5", "us-east-1", MTOK) == Cost(Decimal("0.000000"), True)


def test_formula_hand_computed_vector() -> None:
    m, _ = meter()
    usage = UsageRecord(
        input_tokens=1_234_567, output_tokens=7_654_321, cached_input_tokens=234_567, cache_write_input_tokens=100_000
    )
    # uncached 900,000 x 2.5 = 2,250,000; reads 234,567 x 0.25 = 58,641.75; writes 100,000 x 3.125 (5 m rate, never
    # the 1 h rate) = 312,500; output 7,654,321 x 12.5 = 95,679,012.5; sum / 1e6 = 98.30015425 -> 98.300154
    assert m.price("sol-6.1", "us-east-1", usage) == Cost(Decimal("98.300154"), False)


@pytest.mark.parametrize(("tokens", "usd"), [(1, "0.000000"), (3, "0.000002"), (5, "0.000002"), (7, "0.000004")])
def test_rounding_is_half_even_per_call(tokens: int, usd: str) -> None:
    m, _ = meter()  # 0.5 USD/Mtok: n tokens = n x 0.0000005 exactly half a micro-dollar for odd n
    assert m.price("deepseek-3.2", "us-east-1", UsageRecord(tokens, 0)).usd == Decimal(usd)


def test_tbd_and_missing_components_count_only_with_tokens() -> None:
    m, _ = meter()
    assert m.price("deepseek-3.2", "us-east-1", UsageRecord(10, 0)).estimated is False
    assert m.price("deepseek-3.2", "us-east-1", UsageRecord(10, 1)) == Cost(Decimal("0.000005"), True)  # output TBD
    cached = UsageRecord(input_tokens=10, output_tokens=0, cached_input_tokens=10)  # no cache_read field
    assert m.price("deepseek-3.2", "us-east-1", cached) == Cost(Decimal("0.000000"), True)


def test_estimated_or_inconsistent_usage_is_flagged() -> None:
    m, _ = meter()
    assert m.price("sol-6.1", "us-east-1", UsageRecord(4, 0, tokens_estimated=True)) == Cost(Decimal("0.000010"), True)
    bad = UsageRecord(input_tokens=10, output_tokens=0, cached_input_tokens=8, cache_write_input_tokens=8)
    priced = m.price("sol-6.1", "us-east-1", bad)  # uncached clamped to 0: 8 x 0.25 + 8 x 3.125 = 27 -> 0.000027
    assert priced == Cost(Decimal("0.000027"), True)


def test_an_alias_missing_from_the_current_config_is_priced_tbd(caplog: pytest.LogCaptureFixture) -> None:
    m, _ = meter()  # e.g. a caller without a pinned snapshot whose alias left the current config
    for _ in range(3):  # the sync hot path never raises for it (IMPL-078)
        assert m.price("no-such-model", "us-east-1", MTOK) == Cost(Decimal("0.000000"), True)
    warnings = [r for r in caplog.records if r.name == "noema.ops.cost" and r.levelname == "WARNING"]
    assert len(warnings) == 1 and "no-such-model" in warnings[0].getMessage()  # once per alias


def test_a_pinned_snapshot_keeps_its_catalog_after_an_activation(caplog: pytest.LogCaptureFixture) -> None:
    reg = registry()
    m = LedgerCostMeter(
        FakeClock(wall=datetime(2026, 10, 10, 12, tzinfo=UTC)), reg, FakeOutbox(), FakeTelemetry().instruments
    )
    pinned = reg.current()
    assert reg.activate(registry(price_ref=False).current())  # the new config prices opus by its Bedrock id
    assert m.price("opus-5.5", "us-east-1", MTOK, snapshot=pinned) == Cost(Decimal("20.000000"), False)  # 4 + 16
    assert m.price("opus-5.5", "us-east-1", MTOK) == Cost(Decimal("108.000000"), False)  # current: 9 + 99
    assert m.price("opus-5.5", "us-east-1", MTOK, snapshot=None) == Cost(Decimal("108.000000"), False)
    assert not [r for r in caplog.records if r.name == "noema.ops.cost"]


def test_record_puts_the_row_and_counts_cost() -> None:
    telemetry = FakeTelemetry()
    m, outbox = meter(telemetry=telemetry)
    line = UsageLine(
        id="a" * 26,
        tenant_id="t_acme",
        project_id="prj_1",
        line="waste",
        model_id="deepseek.v3.2",
        region="us-east-1",
        usage=UsageRecord(10, 2, tokens_estimated=True),
        cost=Cost(Decimal("0.000000"), True),
        decision_id="dec_1",
    )
    m.record(line)
    [rec] = outbox.records
    assert (rec.kind, rec.id, rec.tenant_id) == ("usage_line", "a" * 26, "t_acme")
    assert rec.payload["at"] == "2026-10-10T12:00:00Z" and rec.payload["cost_usd"] == "0.000000"
    assert rec.payload["tokens_estimated"] is True and rec.payload["session_id"] is None
    assert telemetry.counter("aghoo_cost_usd_total", tenant="t_acme", line="waste") == 0.0
    m.record(replace(line, cost=Cost(Decimal("1.250000"), False), at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)))
    assert telemetry.counter("aghoo_cost_usd_total", tenant="t_acme", line="waste") == 1.25
    assert outbox.records[1].payload["at"] == "2026-01-02T03:04:05Z"
