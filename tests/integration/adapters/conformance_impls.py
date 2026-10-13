"""Registrations of the real adapters implementations in the conformance suites (spec-02 §14):
- `adapter` / `converse+mantle`: `ConverseAdapter` (lean Converse and ConverseStream) and `MantleAdapter` (lean
  unary chat, httpx streams) behind
  `AdapterRegistryImpl`, answered by the in-process `WireEmulator` (real wire bytes, virtual time) with the
  golden wire capture;
- `adapter` / `converse+mantle@mockbedrock`: the same adapters over real sockets against qa-tools' independent wire
  mock (`tools/mockbedrock`, spec-10 §2) in wall time (`virtual_time=False`: the suite skips its timing
  assertions); the golden capture is the mock's own request log (see `mock_wire`);
- `health_registry` / `breakers+budget`: `BreakerRegistry` and `SlidingRetryBudget` on the harness clock.
The cross-pod Redis sync of both is covered by `tests/integration/adapters/test_resilience_redis.py`."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import pytest
from tests.contract_internal.kits import AdapterKit, HealthKit
from tests.contract_internal.registry import Harness, register
from tests.fakes.clock import FakeClock
from tests.fakes.config import FakeConfigRegistry
from tests.fakes.credentials import FakeBroker
from tests.golden.wire import load_wire_goldens
from tests.unit.adapters.support import rig
from tools.mockbedrock import MockBedrock

from noema.adapters.converse import ConverseAdapter
from noema.adapters.mantle import MantleAdapter
from noema.adapters.registry import AdapterRegistryImpl
from noema.adapters.resilience.breakers import BreakerRegistry
from noema.adapters.resilience.retry_budget import RetryBudgetPolicy, SlidingRetryBudget
from noema.core.clock import SystemClock

Json = dict[str, Any]
_CONVERSE_OPS = frozenset({"converse", "converse-stream"})
_MODEL_OPS = _CONVERSE_OPS | {"chat", "chat-stream"}


@register("adapter", "converse+mantle")
@asynccontextmanager
async def real_adapters(h: Harness) -> AsyncIterator[AdapterKit]:
    async with rig(clock=h.clock) as r:
        yield AdapterKit(
            adapters=r.registry,
            clock=h.clock,
            tenant=r.tenant,
            use_scenario=r.emulator.use_scenario,
            wire_requests=lambda: list[Mapping[str, Any]](r.emulator.wire),
            open_streams=lambda: r.converse.open_streams + r.mantle.open_streams + r.emulator.open_streams,
            virtual_time=True,
        )


class WallClock(FakeClock):
    """The kit clock of the wire-mock kit: a `FakeClock` (the kit's type) that follows the system clock. The mock
    serves real sockets in wall time, and a virtual clock cannot see socket I/O (its autojump would race it), so
    `autojump()` only yields and every limit runs in real time."""

    def __init__(self) -> None:
        super().__init__()
        self._system = SystemClock()

    def now(self) -> datetime:
        return self._system.now()

    def monotonic(self) -> float:
        return self._system.monotonic()

    async def sleep(self, seconds: float) -> None:
        await self._system.sleep(seconds)

    @asynccontextmanager
    async def autojump(self) -> AsyncIterator[FakeClock]:
        yield self


class _SentBlobs:
    """The bytes the Converse client sent, by SHA-256 (the mock logs every blob as `{"$bytes_len", "sha256"}`):
    every blob of the golden requests, known up front (the lean Converse path has no botocore parameter events)."""

    def __init__(self) -> None:
        self.by_digest: dict[str, bytes] = {}
        for golden in load_wire_goldens():
            self._walk_b64(golden["request"])

    def _walk_b64(self, node: object) -> None:
        if isinstance(node, Mapping):
            if "$bytes_b64" in node:
                data = base64.b64decode(str(node["$bytes_b64"]))
                self.by_digest[hashlib.sha256(data).hexdigest()] = data
            for value in node.values():
                self._walk_b64(value)
        elif isinstance(node, list):
            for value in node:
                self._walk_b64(value)

    def golden(self, node: Any) -> Any:
        """A logged body with each blob summary the client sent back in the golden form `{"$bytes_b64"}`."""
        if isinstance(node, Mapping):
            data = self.by_digest.get(str(node.get("sha256")))
            if set(node) == {"$bytes_len", "sha256"} and data is not None and len(data) == node["$bytes_len"]:
                return {"$bytes_b64": base64.b64encode(data).decode()}
            return {k: self.golden(v) for k, v in node.items()}
        if isinstance(node, list):
            return [self.golden(v) for v in node]
        return node


def mock_wire(mock: MockBedrock, blobs: _SentBlobs) -> list[Mapping[str, Any]]:
    """The request bodies the mock received (oldest first) in the golden JSON form: Converse bodies get their
    `modelId` back from the URL path, logged blob digests become the bytes the client sent."""
    out: list[Mapping[str, Any]] = []
    for record in mock.requests():
        if record["op"] not in _MODEL_OPS or record["body"] is None:
            continue
        body: Json = blobs.golden(record["body"])
        out.append({"modelId": record["model"], **body} if record["op"] in _CONVERSE_OPS else body)
    return out


@register("adapter", "converse+mantle@mockbedrock", marks=(pytest.mark.integration,))
@asynccontextmanager
async def adapters_against_the_wire_mock(h: Harness) -> AsyncIterator[AdapterKit]:
    clock, config, blobs = WallClock(), FakeConfigRegistry(), _SentBlobs()
    broker = FakeBroker(clock, config, mantle_mode="sigv4")  # the mock decodes only real short-term tokens

    def use_scenario(doc: Mapping[str, Any]) -> None:
        mock.load_scenario({"mantle_auth": "sigv4", **doc})

    async with MockBedrock() as mock:
        common: Json = {
            "config": config,
            "broker": broker,
            "clock": clock,
            # the mock's connect_reset is a TCP RST after the request write (possibly delivered): budgeted under R8,
            # which has no floor, so the suite's connect-retry row gets one re-send on its quiet key
            "retry_budget": SlidingRetryBudget(clock, policy=RetryBudgetPolicy(min_retries=1)),
            "endpoint_overrides": {"bedrock-runtime": mock.endpoint_url, "mantle": mock.mantle_url},
        }
        converse, mantle = ConverseAdapter(**common), MantleAdapter(**common)
        registry = AdapterRegistryImpl(config, {"bedrock-converse": converse, "bedrock-mantle-chat": mantle})
        try:
            yield AdapterKit(
                adapters=registry,
                clock=clock,
                tenant=config.current().tenant_ref("t_acme"),
                use_scenario=use_scenario,
                wire_requests=lambda: mock_wire(mock, blobs),
                open_streams=lambda: converse.open_streams + mantle.open_streams,
                virtual_time=False,
            )
        finally:
            await converse.aclose()
            await mantle.aclose()


@register("health_registry", "breakers+budget")
@asynccontextmanager
async def real_health(h: Harness) -> AsyncIterator[HealthKit]:
    yield HealthKit(health=BreakerRegistry(h.clock), retry_budget=SlidingRetryBudget(h.clock), clock=h.clock)
