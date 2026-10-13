"""Fixtures of the contract QA track (tests/qa/contract, `make qa-contract`; Wave E).

The stack tests run against a compose stack of their own (`make qa-contract` starts `noema-qa-contract` on port base
29600; `NOEMA_E2E_STATE` / `NOEMA_E2E_PROJECT` select another). They reuse the e2e fixtures (`stack`, `mock`,
tests/e2e/conftest.py), whose `CheckedClient` validates every response with the `ContractValidator`; the
schemathesis tests are synchronous (hypothesis) and use `sync_stack` instead. The docs-drift and SDK-compatibility
tests need no stack.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx
import orjson
import pytest
from tests.e2e.conftest import DEFAULT_SCENARIO, Mock, Stack, _state_path
from tests.e2e.conftest import mock as mock  # re-exported fixtures
from tests.e2e.conftest import stack as stack
from tests.e2e.suite.multipod import heal_circuits
from tests.qa.contract.fuzzkit import VALIDATOR

READY_TIMEOUT_S = 60.0


def _unavailable(reason: str) -> None:
    if os.environ.get("NOEMA_E2E_REQUIRE") == "1":
        pytest.fail(f"contract stack unavailable: {reason}")
    pytest.skip(f"contract stack unavailable ({reason}): make qa-contract")


@pytest.fixture(scope="session")
def sync_stack() -> dict[str, Any]:
    """The bootstrap state of the stack (synchronous; both replicas answered /readyz 200)."""
    path = _state_path()
    try:
        state: dict[str, Any] = orjson.loads(path.read_bytes())
    except (FileNotFoundError, orjson.JSONDecodeError) as exc:
        _unavailable(f"no bootstrap state at {path} ({type(exc).__name__})")
        raise AssertionError from exc  # unreachable: _unavailable skips or fails
    with httpx.Client(timeout=5.0) as http:
        for base in state["endpoints"]["api"][:2]:
            deadline = time.monotonic() + READY_TIMEOUT_S
            while True:
                try:
                    ready = http.get(f"{base}/readyz")
                    VALIDATOR.check_response("readyz", ready.status_code, dict(ready.headers), ready.content)
                    if ready.status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                if time.monotonic() > deadline:
                    _unavailable(f"{base} is not ready after {READY_TIMEOUT_S:.0f} s")
                time.sleep(0.5)
    return state


@pytest.fixture
async def default_scenario(stack: Stack, mock: Mock) -> None:
    """The stack's default mock scenario and every service circuit closed on both replicas (tests/e2e/suite)."""
    await mock.load(DEFAULT_SCENARIO.read_text())
    await heal_circuits(stack)
