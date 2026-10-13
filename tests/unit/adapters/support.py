"""Test rig for the adapters: the real `ConverseAdapter` / `MantleAdapter` wired to the in-process `WireEmulator`,
a `FakeBroker` and the real `SlidingRetryBudget`, all on one `FakeClock` (virtual time) unless `adapter_clock` runs
the adapters, broker and budget on another clock (e.g. `SystemClock`)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal

import anyio
import anyio.lowlevel
import httpx
from tests.fakes.clock import FakeClock
from tests.fakes.config import FakeConfigRegistry, patched_example, snapshot_from
from tests.fakes.credentials import FakeBroker
from tests.fakes.telemetry import FakeTelemetry
from tests.unit.adapters.emulator import WireEmulator

from noema.adapters._base import SecurityEventSink
from noema.adapters._unary import RawResponse, StreamedResponse, StreamTransport
from noema.adapters.converse import ConverseAdapter
from noema.adapters.mantle import MantleAdapter
from noema.adapters.registry import AdapterRegistryImpl
from noema.adapters.resilience.retry_budget import SlidingRetryBudget
from noema.core.clock import Clock, Deadline
from noema.core.principal import TenantRef
from noema.core.protocols.adapters import ModelRequest, StreamEvent, StreamTimeouts
from noema.core.protocols.credentials import TenantCredentials
from noema.core.values import CallKind, Message, TextBlock

TIMEOUTS = StreamTimeouts(connect_s=3, first_event_s=5, first_answer_s=8, idle_s=30)
SOL, GLM, OPUS, HAIKU, DEEPSEEK = (
    "us.openai.gpt-6.1-sol",
    "us.zai.glm-5.3",
    "us.anthropic.claude-opus-5-5",
    "us.anthropic.claude-haiku-5-5",
    "deepseek.v3.2",
)
OTHER_TENANT = {
    "aws_account_id": "444455556666",
    "invoke_role_arn": "arn:aws:iam::444455556666:role/AghooBedrockInvoke",
    "external_id_ref": "aws-sm://platform/tenants/t_beta/external-id",
    "home_region": "us-west-2",
    "defaults": {"decision_profile": "lightweight-model-decide", "generation_profile": "sol-fast"},
}


@dataclass
class Rig:
    clock: FakeClock
    config: FakeConfigRegistry
    broker: FakeBroker
    budget: SlidingRetryBudget
    telemetry: FakeTelemetry
    emulator: WireEmulator
    converse: ConverseAdapter
    mantle: MantleAdapter
    registry: AdapterRegistryImpl
    tenant: TenantRef

    def deadline(self, s: float = 60.0) -> Deadline:
        return Deadline(self.clock.monotonic() + s, self.clock)

    def scenario(self, *rules: Mapping[str, Any], seed: int = 3) -> None:
        self.emulator.use_scenario(
            {"scenario": "adapters", "seed": seed, "defaults": {"latency": {"fixed_ms": 0}}, "rules": list(rules)}
        )

    def metric(self, name: str, **labels: str) -> float:
        """The sum of a counter's recorded values (labels filter) or a gauge's last value."""
        values = [
            m.value
            for m in self.telemetry.metrics
            if m.name == name and all(m.labels.get(k) == v for k, v in labels.items())
        ]
        if not values:
            return 0.0
        kind = next(m.kind for m in self.telemetry.metrics if m.name == name)
        return values[-1] if kind == "gauge" else sum(values)


@asynccontextmanager
async def rig(
    *,
    clock: FakeClock | None = None,
    adapter_clock: Clock | None = None,
    mantle_mode: Literal["sigv4", "bearer_short_term"] = "bearer_short_term",
    two_tenants: bool = False,
    converse_unary: Callable[[TenantCredentials], StreamTransport] | None = None,
    mantle_transport: httpx.AsyncBaseTransport | None = None,
    budget: SlidingRetryBudget | None = None,
    security: SecurityEventSink | None = None,
) -> AsyncIterator[Rig]:
    """The real adapters on the emulator (see the module docstring); closed at exit. With `adapter_clock` the
    emulator stays on `clock` (plan events at 0 ms need no time to pass) and `Rig.deadline()` is not meaningful."""
    clock = clock or FakeClock()
    aclock = adapter_clock or clock
    snapshot = snapshot_from(patched_example({"tenants": {"t_beta": OTHER_TENANT}})) if two_tenants else None
    config = FakeConfigRegistry(snapshot)
    broker = FakeBroker(aclock, config, mantle_mode=mantle_mode)
    budget = budget or SlidingRetryBudget(aclock)
    telemetry = FakeTelemetry()
    emulator = WireEmulator(clock)
    common: dict[str, Any] = {
        "config": config,
        "broker": broker,
        "clock": aclock,
        "retry_budget": budget,
        "telemetry": telemetry,
        "security": security,
    }
    converse = ConverseAdapter(**common, unary_transport=converse_unary or emulator.converse_unary)
    mantle = MantleAdapter(**common, transport=mantle_transport or emulator.mantle_transport())
    registry = AdapterRegistryImpl(config, {"bedrock-converse": converse, "bedrock-mantle-chat": mantle})
    tenant = config.current().tenant_ref("t_acme")
    try:
        yield Rig(clock, config, broker, budget, telemetry, emulator, converse, mantle, registry, tenant)
    finally:
        await converse.aclose()
        await mantle.aclose()


def request(
    alias: str,
    text: str = "Hi",
    *,
    kind: CallKind = "generation",
    retry: Literal["none", "default"] = "default",
    max_tokens: int = 64,
    **kw: Any,
) -> ModelRequest:
    return ModelRequest(
        alias, ("You are terse.",), (Message("user", (TextBlock(text),)),), max_tokens, kind, retry, **kw
    )


async def collect(
    r: Rig,
    alias: str,
    req: ModelRequest | None = None,
    *,
    timeouts: StreamTimeouts = TIMEOUTS,
    deadline_s: float = 60.0,
    tenant: TenantRef | None = None,
) -> list[tuple[int, StreamEvent]]:
    """Run one stream to its end under autojump: (ms since the call, event)."""
    out: list[tuple[int, StreamEvent]] = []
    t0 = r.clock.monotonic()
    async with r.clock.autojump():
        cm = r.registry.for_model(alias).stream(
            req or request(alias), tenant=tenant or r.tenant, timeouts=timeouts, deadline=r.deadline(deadline_s)
        )
        async with cm as events:
            out.extend([(round((r.clock.monotonic() - t0) * 1000), ev) async for ev in events])
    return out


def dist_answer(dist: Mapping[str, float]) -> str:
    return json.dumps(dist)


class _Lines(httpx.AsyncByteStream):
    def __init__(self, lines: Sequence[bytes], hang: bool) -> None:
        self.lines, self.hang, self.closed = lines, hang, False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for line in self.lines:
            await anyio.lowlevel.checkpoint()
            yield line
        if self.hang:
            await anyio.sleep_forever()

    async def aclose(self) -> None:
        self.closed = True


class Scripted:
    """A Mantle transport answering every request with one scripted response."""

    def __init__(
        self,
        lines: Sequence[bytes] = (),
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
        hang: bool = False,
    ) -> None:
        self.status, self.headers, self.body = status, headers or {}, body
        self.stream = _Lines(lines, hang)
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        async def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.status != 200 or self.body:
                return httpx.Response(self.status, headers=self.headers, content=self.body or b"{}")
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=self.stream)

        return httpx.MockTransport(handle)


class Unary:
    """A unary (Converse) transport factory answering every POST with `respond(n, url, headers, body)` (n = the
    call's 1-based number across all clients); `clients` lists the transports created (one per pool key)."""

    def __init__(self, respond: Callable[[int, str, Mapping[str, str], bytes], RawResponse]) -> None:
        self.respond = respond
        self.clients: list[TenantCredentials] = []
        self.calls: list[tuple[str, Mapping[str, str], bytes]] = []

    def __call__(self, creds: TenantCredentials) -> StreamTransport:
        self.clients.append(creds)
        owner = self

        class _Client:
            async def post(self, url: str, headers: Mapping[str, str], body: bytes, *, connect_s: float) -> RawResponse:
                await anyio.lowlevel.checkpoint()
                owner.calls.append((url, headers, body))
                return owner.respond(len(owner.calls), url, headers, body)

            async def open(
                self, url: str, headers: Mapping[str, str], body: bytes, *, connect_s: float
            ) -> StreamedResponse:
                raise NotImplementedError("a scripted unary transport answers Converse calls only")

            async def aclose(self) -> None:
                return None

        return _Client()


def json_reply(body: Mapping[str, Any], status: int = 200, headers: Mapping[str, str] | None = None) -> RawResponse:
    return RawResponse(status, {"content-type": "application/json", **(headers or {})}, json.dumps(body).encode())
