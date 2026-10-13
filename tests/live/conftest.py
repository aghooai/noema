"""Live smoke (spec-10 §6): real Bedrock calls, only with `NOEMA_LIVE=1` (the root conftest skips `live` tests
otherwise), hard-stopped at 87 calls across EVERY run (the journal `.orchestration/reports/live-calls.jsonl`).
The hard stop was 60 for the Wave E smoke (57 used); owner approval of 2026-10-12 (IMPL-080 amendment, spec-10 §6):
+30 calls for the Wave E3 re-verification, so `budget.LIMIT` = 87 ledger entries in total, never more.

Run (QA, Wave E; never in `make check`):
`NOEMA_LIVE=1 UV_NO_SYNC=1 uv run --frozen --no-sync pytest -p tests.live.bearer tests/live -m live -x`.
`-p tests.live.bearer` keeps the shell's `AWS_BEARER_TOKEN_BEDROCK` (the root conftest scrubs `AWS_*`) for the
API-level turn on identity's `AmbientBroker`. Every test module carries `pytest.mark.live` and
`pytest.mark.enable_socket` (pytest-socket allows only loopback otherwise). Credentials: `tests/live/ambient.py`
(the shared credentials file, read once). Region us-east-1, passed explicitly.

- Refusal: a live session does not start when the journal already holds >= 87 calls, or without the bearer token.
- `ledger.need(n)` (first statement of every calling test): its worst-case call count; the test is skipped (nothing
  sent) when fewer than `n` calls are left in the global budget.
- Rehearsal: `NOEMA_LIVE_MOCK=1` (with `NOEMA_LIVE=1`) runs the same tests against an in-process wire mock
  (`tools/mockbedrock`, scenario `tests/live/mock_scenario.yaml`, probe latencies): no Bedrock call, no journal,
  cassettes under `tests/cassettes/mock-<run>/` for the mock-fidelity diff (`tests/live/fidelity.py`).
- `ledger` (session): the process `Ledger` (allowlist, max_tokens 256 / 512 for generation, redacted cassettes
  with wire-shape taps); `reports/live/calls.json` is written at session end. A `LiveBudgetExceeded` stops the
  session.
- `live` (session): the real `ConverseAdapter` / `MantleAdapter` on the ambient broker behind
  `CountingAdapterRegistry`, the live config (`tests/live/config/`), the tenant `t_dev`, the real estimator (K1).
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator, Generator
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest
from conftest import NoemaServices
from tests.fakes.config import FakeConfigRegistry
from tests.live import bearer, wiretap
from tests.live.ambient import LiveAmbientBroker
from tests.live.budget import JOURNAL, LIMIT, CountingAdapterRegistry, Ledger, LiveBudgetExceeded, journal_entries
from tests.live.livecfg import LIVE_TENANT, live_snapshot
from tools.mockbedrock import MockBedrock

from noema.adapters.converse import ConverseAdapter
from noema.adapters.mantle import MantleAdapter
from noema.adapters.registry import AdapterRegistryImpl
from noema.adapters.resilience.retry_budget import SlidingRetryBudget
from noema.core.clock import Deadline, SystemClock
from noema.core.principal import TenantRef
from noema.ops.cost.estimator import HeuristicTokenEstimator

MOCK: Final = os.environ.get("NOEMA_LIVE_MOCK") == "1"
SCENARIO: Final = Path(__file__).parent / "mock_scenario.yaml"
MOCK_CREDENTIALS: Final = (
    "[default]\naws_access_key_id = AKIAMOCKREHEARSAL000\naws_secret_access_key = mock-rehearsal-secret\n"
    "aws_session_token = mock-rehearsal-session-token\n"
)
_LEDGER = Ledger(run=time.strftime("%Y%m%dT%H%M%S"), mode="mock" if MOCK else "live", journal=None if MOCK else JOURNAL)


def pytest_configure(config: pytest.Config) -> None:
    if os.environ.get("NOEMA_LIVE") != "1" or MOCK:
        return
    used = journal_entries(JOURNAL)
    if used >= LIMIT:
        pytest.exit(f"live smoke refused: the journal {JOURNAL} already holds {used} >= {LIMIT} calls", 3)
    if bearer.TOKEN is None:
        pytest.exit("live smoke refused: run with `-p tests.live.bearer` and AWS_BEARER_TOKEN_BEDROCK set", 3)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    if call.excinfo is not None and call.excinfo.errisinstance(LiveBudgetExceeded):
        item.session.shouldstop = "live call budget exceeded (spec-10 §6)"
    return report


def pytest_sessionfinish(session: pytest.Session) -> None:
    if _LEDGER.count or _LEDGER.calls:
        _LEDGER.write_report()


@pytest.fixture(scope="session")
def ledger() -> Ledger:
    wiretap.install()
    return _LEDGER


@dataclass
class Live:
    """The live rig: counted adapters, the broker (its `mantle_mode` is switchable), config and tenant."""

    adapters: CountingAdapterRegistry
    converse: ConverseAdapter
    mantle: MantleAdapter
    broker: LiveAmbientBroker
    config: FakeConfigRegistry
    tenant: TenantRef
    clock: SystemClock
    ledger: Ledger
    credentials_file: str  # the ambient chain's shared credentials file (the rehearsal: fake keys)
    sts_endpoint: str | None  # None = AWS STS; the rehearsal: moto

    def deadline(self, s: float = 120.0) -> Deadline:
        return Deadline(self.clock.monotonic() + s, self.clock)


def _ambient_source(services: NoemaServices, tmp: pytest.TempPathFactory) -> tuple[str, str | None]:
    """(shared credentials file, STS endpoint). The rehearsal's mock needs a session token (STS-shaped credentials)
    and verifies no signature: fake keys, and the caller account from moto STS."""
    if not MOCK:
        return os.path.expanduser(os.environ.get("NOEMA_LIVE_AWS_CREDENTIALS_FILE", "~/.aws/credentials")), None
    path = tmp.mktemp("mock-aws") / "credentials"
    path.write_text(MOCK_CREDENTIALS)
    return str(path), services.moto_url


@pytest.fixture(scope="session")
async def live(
    ledger: Ledger, noema_services: NoemaServices, tmp_path_factory: pytest.TempPathFactory
) -> AsyncIterator[Live]:
    if os.environ.get("NOEMA_LIVE") != "1":
        pytest.skip("live Bedrock test: set NOEMA_LIVE=1")
    clock = SystemClock()
    config = FakeConfigRegistry()
    creds, sts = _ambient_source(noema_services, tmp_path_factory)
    broker = LiveAmbientBroker(
        clock, config, credentials_file=creds, profile="default" if MOCK else None, sts_endpoint=sts
    )
    await broker.start()
    assert broker.caller_account is not None
    config.activate(live_snapshot(broker.caller_account))
    ledger.estimator = HeuristicTokenEstimator()  # K1 (test_zz_estimator.py)
    async with AsyncExitStack() as stack:
        common: dict[str, Any] = {"config": config, "broker": broker, "clock": clock}
        if MOCK:
            mock = await stack.enter_async_context(MockBedrock(scenario=SCENARIO))
            common["endpoint_overrides"] = {"bedrock-runtime": mock.endpoint_url, "mantle": mock.mantle_url}
        converse = ConverseAdapter(**common, retry_budget=SlidingRetryBudget(clock))
        mantle = MantleAdapter(**common, retry_budget=SlidingRetryBudget(clock))
        stack.push_async_callback(converse.aclose)
        stack.push_async_callback(mantle.aclose)
        inner = AdapterRegistryImpl(config, {"bedrock-converse": converse, "bedrock-mantle-chat": mantle})
        tenant = config.current().tenant_ref(LIVE_TENANT)
        counted = CountingAdapterRegistry(inner, ledger)
        yield Live(counted, converse, mantle, broker, config, tenant, clock, ledger, creds, sts)
