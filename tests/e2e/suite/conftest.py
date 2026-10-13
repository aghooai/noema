"""Fixtures of the full e2e suite (tests/e2e/suite), on top of tests/e2e/conftest.py (`stack`, `mock`).

Each test starts from the stack's default mock scenario with every service circuit closed on both replicas, and
loads its own scenario; `mock` restores the default afterwards. The breakers outlive a test and are shared by both
pods (breaker_events), so a test that trips one on purpose (G09, the multipod Haiku burst) would otherwise leave
the next test's members `circuit_open`. The stack runs with the p95-TTFT rule disarmed (`make e2e-up`:
NOEMA_BREAKER_TTFT_RULE=off), since the scenarios change a model's latency up to 30x between tests.
The tests share one stack and one mock, so they run serially (no xdist).
"""

from __future__ import annotations

import pytest
from tests.e2e.conftest import DEFAULT_SCENARIO, Mock, Stack
from tests.e2e.suite.multipod import heal_circuits


@pytest.fixture(autouse=True)
async def default_scenario(stack: Stack, mock: Mock) -> None:
    """The stack's default scenario, whatever an earlier test or run loaded, and no circuit open or half-open."""
    await mock.load(DEFAULT_SCENARIO.read_text())
    await heal_circuits(stack)
