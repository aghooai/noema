"""ops-ledger registrations of the in-memory conformance suites (spec-02 §14): `cost_meter`, `token_estimator`."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from tests.contract_internal.kits import CostKit, EstimatorKit
from tests.contract_internal.registry import Harness, register
from tests.fakes.outbox import FakeOutbox
from tests.fakes.telemetry import FakeTelemetry

from noema.core.protocols.config import ConfigRegistry
from noema.core.protocols.storage import OutboxRecord
from noema.ops.cost import HeuristicTokenEstimator, LedgerCostMeter


@register("cost_meter", "ledger")
@asynccontextmanager
async def ledger_cost_meter(h: Harness) -> AsyncIterator[CostKit]:
    def make(config: ConfigRegistry) -> tuple[LedgerCostMeter, Callable[[], list[OutboxRecord]]]:
        outbox = FakeOutbox()
        meter = LedgerCostMeter(h.clock, config, outbox, FakeTelemetry().instruments)
        return meter, lambda: list(outbox.records)

    yield CostKit(make)


@register("token_estimator", "heuristic")
@asynccontextmanager
async def heuristic_estimator(h: Harness) -> AsyncIterator[EstimatorKit]:
    # The shared vectors pin the IMPL-029c arithmetic (chars/4, no overhead): the empty table. The per-model fits
    # (Wave E2 defect 32) are tested in tests/unit/ops_ledger/test_estimator.py.
    yield EstimatorKit(HeuristicTokenEstimator(table={}))
