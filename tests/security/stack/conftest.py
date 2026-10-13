"""Fixtures of the stack-level security tests (tests/security/stack, `make qa-security`): the e2e `stack` and `mock`
fixtures (tests/e2e/conftest.py; every Noema response contract-validated by `CheckedClient`), on the security
stack (`NOEMA_E2E_PROJECT=noema-sec`, its own ports). Each test starts from the default mock scenario with every
circuit closed (as the e2e suite does); the tests share one stack and one mock, so they run serially."""

from __future__ import annotations

import pytest
from tests.e2e.conftest import DEFAULT_SCENARIO, Mock, Stack, mock, stack
from tests.e2e.suite.multipod import heal_circuits

__all__ = ["mock", "stack"]


@pytest.fixture(autouse=True)
async def default_scenario(stack: Stack, mock: Mock) -> None:
    await mock.load(DEFAULT_SCENARIO.read_text())
    await heal_circuits(stack)
