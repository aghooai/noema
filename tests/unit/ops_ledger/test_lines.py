"""T-COST-2, ledger side (spec-11 §2, IMPL-020): deterministic line ids per recorder and granularity, and the
`usage_line` payload; Σ of per-call rounded costs equals the line cost (the line sums per-call values).

The recorder side (decision engine, orchestrator, labeling) asserts Σ lines = `DecisionResult.usage` /
`turn.completed.usage` in their own suites; the persistence side (replays exactly once, the report sums) is in
tests/integration/ops_ledger/test_usage_ledger.py::test_recorded_lines_land_once_and_sum_exactly.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from tests.fakes.clock import FakeClock
from tests.fakes.config import FakeConfigRegistry, patched_example
from tests.fakes.outbox import FakeOutbox
from tests.fakes.telemetry import FakeTelemetry

from noema.config.loader import load_config
from noema.core.protocols.ops import UsageLine
from noema.core.values import Cost, UsageRecord
from noema.ops.cost import (
    LedgerCostMeter,
    attempt_line_id,
    cache_savings_line_id,
    decision_line_id,
    labeling_line_id,
    rfc3339,
    usage_line_payload,
)

pytestmark = pytest.mark.unit

HEX26 = re.compile(r"^[0-9a-f]{26}$")


def test_ids_are_the_spec_hashes() -> None:
    assert decision_line_id("dec_01J0", "decision", "deepseek.v3.2") == "62c5d156b60eedc5f7965a06db"
    assert cache_savings_line_id("dec_01J0") == "5062acbd48f72a8f35dfb3d3ff"
    assert attempt_line_id("att_01J0", "generation") == "51d8d7571838cadf7ac4e92db1"
    assert labeling_line_id("job_01J0", 2, "q0", "us.anthropic.claude-haiku-5-5") == "b07df26e09b1f6a36bdd2a7c89"


def test_one_id_per_granule() -> None:
    ids = {
        decision_line_id("dec_1", "decision", "deepseek.v3.2"),
        decision_line_id("dec_1", "decision", "us.openai.gpt-6-luna"),
        decision_line_id("dec_1", "waste", "deepseek.v3.2"),
        decision_line_id("dec_1", "verification", "deepseek.v3.2"),
        decision_line_id("dec_2", "decision", "deepseek.v3.2"),
        cache_savings_line_id("dec_1"),
        attempt_line_id("att_1", "generation"),
        attempt_line_id("att_1", "waste"),
        labeling_line_id("job_1", 1, "q0", "m"),
        labeling_line_id("job_1", 2, "q0", "m"),
    }
    assert len(ids) == 10 and all(HEX26.match(i) for i in ids)
    assert decision_line_id("dec_1", "decision", "deepseek.v3.2") == decision_line_id(
        "dec_1", "decision", "deepseek.v3.2"
    )


def test_rfc3339_is_utc_z() -> None:
    assert rfc3339(datetime(2026, 10, 10, 14, 0, tzinfo=timezone(timedelta(hours=2)))) == "2026-10-10T12:00:00Z"
    naive = datetime(2026, 10, 10, 12, 0, 0, 1500, tzinfo=UTC).replace(tzinfo=None)  # naive = UTC
    assert rfc3339(naive) == "2026-10-10T12:00:00.001500Z"


def test_payload_is_the_usage_line_row() -> None:
    line = UsageLine(
        id="x" * 26,
        tenant_id="t_acme",
        project_id="prj_1",
        line="cache_savings",
        model_id=None,
        region=None,
        usage=UsageRecord(100, 5, cached_input_tokens=0, cache_write_input_tokens=0, reasoning_tokens=3),
        cost=Cost(Decimal("1.5"), False),
        decision_id="dec_1",
        session_id="ses_1",
    )
    payload = usage_line_payload(line, datetime(2026, 10, 10, 12, tzinfo=UTC))
    assert payload == {
        "tenant_id": "t_acme",
        "project_id": "prj_1",
        "id": "x" * 26,
        "line": "cache_savings",
        "model_id": None,
        "region": None,
        "session_id": "ses_1",
        "turn_id": None,
        "decision_id": "dec_1",
        "attempt_id": None,
        "labeling_run_id": None,
        "input_tokens": 100,
        "output_tokens": 5,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "reasoning_tokens": 3,
        "tokens_estimated": False,
        "cost_usd": "1.5",
        "cost_estimated": False,
        "at": "2026-10-10T12:00:00Z",
    }


def test_a_line_sums_per_call_rounded_costs() -> None:
    """Each call is rounded on its own and the line carries the sum, so Σ line costs equals Σ call costs exactly
    (rounding the summed usage once would differ by up to half a micro-dollar per call)."""
    catalog = {
        "version": 1,
        "entries": [
            {
                "model": "deepseek.v3.2",
                "region": "us-east-1",
                "effective_from": "2026-01-01",
                "usd_per_mtok": {"input": "0.5", "output": "0.5"},
            }
        ],
    }
    body = patched_example({"platform": {"cost": {"prices_ref": "db:1"}}})
    config = FakeConfigRegistry(load_config(body, env="test", prices=lambda v: catalog if v == 1 else None))
    meter = LedgerCostMeter(FakeClock(), config, FakeOutbox(), FakeTelemetry().instruments)
    calls = [UsageRecord(1, 0), UsageRecord(1, 0), UsageRecord(1, 0)]  # 0.5 micro-dollars each
    costs = [meter.price("deepseek-3.2", "us-east-1", u) for u in calls]
    assert [c.usd for c in costs] == [Decimal("0.000000")] * 3  # each rounds half-even to 0
    line_cost = sum((c.usd for c in costs), Decimal(0))
    once = meter.price("deepseek-3.2", "us-east-1", calls[0] + calls[1] + calls[2])  # 1.5 -> 2 micro-dollars
    assert line_cost == Decimal("0.000000") and once.usd == Decimal("0.000002")
