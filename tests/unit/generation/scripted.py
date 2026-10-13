"""A scripted `Adapter` for engine unit tests: each `stream()` call of an alias plays the next `Call` of its script
on the FakeClock (events at fixed offsets from the open), and records what the engine asked for."""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field

from tests.fakes.adapters import capabilities_of

from noema.core.clock import Clock, Deadline
from noema.core.errors import ClassifiedError, ErrorKind, TenantAccessCause
from noema.core.principal import TenantRef
from noema.core.protocols.adapters import (
    AdapterError,
    ErrorEvent,
    MessageStart,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    ReasoningDelta,
    Stop,
    StopReason,
    StreamEvent,
    StreamTimeouts,
    TextDelta,
    UsageEvent,
)
from noema.core.protocols.config import ConfigSnapshot
from noema.core.values import UsageRecord


@dataclass
class Call:
    """One scripted stream: `open_error` raises from `__aenter__` after `open_s`; otherwise `events` are yielded
    at their offsets (seconds after the open) and, with `hang`, the stream then blocks until cancelled."""

    events: Sequence[tuple[float, StreamEvent]] = ()
    open_error: ClassifiedError | None = None
    open_s: float = 0.0
    hang: bool = False


@dataclass
class Opened:
    """What one call received."""

    request: ModelRequest
    timeouts: StreamTimeouts
    deadline: Deadline
    at: float
    closed: bool = False


@dataclass
class ScriptedAdapter:
    """Implements `Adapter` (stream only; `complete` is never used by generation)."""

    clock: Clock
    snapshot: ConfigSnapshot
    scripts: dict[str, list[Call]] = field(default_factory=dict)
    calls: list[Opened] = field(default_factory=list)
    default: Call | None = None  # played once an alias' script is used up (None: AssertionError)

    def capabilities(self, alias: str) -> ModelCapabilities:
        return capabilities_of(self.snapshot.model(alias))

    def classify_error(self, exc: BaseException) -> ClassifiedError:
        return ClassifiedError(ErrorKind.FATAL, "unexpected", type(exc).__name__)

    async def complete(self, req: ModelRequest, *, tenant: TenantRef, deadline: Deadline) -> ModelResponse:
        raise AssertionError("generation never calls complete()")

    def stream(
        self, req: ModelRequest, *, tenant: TenantRef, timeouts: StreamTimeouts, deadline: Deadline
    ) -> AbstractAsyncContextManager[AsyncIterator[StreamEvent]]:
        return self._stream(req, timeouts, deadline)

    @asynccontextmanager
    async def _stream(
        self, req: ModelRequest, timeouts: StreamTimeouts, deadline: Deadline
    ) -> AsyncIterator[AsyncIterator[StreamEvent]]:
        script = self.scripts.get(req.model_alias) or []
        if not script and self.default is None:
            raise AssertionError(f"no scripted call left for {req.model_alias}")
        call = script.pop(0) if script else self.default
        assert call is not None
        opened = Opened(req, timeouts, deadline, self.clock.monotonic())
        self.calls.append(opened)
        if call.open_s:
            await self.clock.sleep(call.open_s)
        if call.open_error is not None:
            raise AdapterError(call.open_error)
        t0 = self.clock.monotonic()
        try:
            yield self._events(call, t0)
        finally:
            opened.closed = True

    async def _events(self, call: Call, t0: float) -> AsyncGenerator[StreamEvent]:
        for at, ev in call.events:
            await self.clock.sleep(max(0.0, t0 + at - self.clock.monotonic()))
            yield ev
        if call.hang:
            await self.clock.sleep(10_000)


@dataclass
class ScriptedRegistry:
    """Implements `AdapterRegistry` with one scripted adapter for every alias."""

    adapter: ScriptedAdapter

    def for_model(self, alias: str) -> ScriptedAdapter:
        return self.adapter


def text_stream(
    *chunks: str,
    first_s: float = 0.2,
    every_s: float = 0.1,
    stop: StopReason | None = "end_turn",
    usage: UsageRecord | None = None,
    start: bool = True,
) -> list[tuple[float, StreamEvent]]:
    """MessageStart at 0.05 s, then the chunks from `first_s` every `every_s`, then Stop and UsageEvent."""
    events: list[tuple[float, StreamEvent]] = [(0.05, MessageStart(at=0.0))] if start else []
    t = first_s
    for chunk in chunks:
        events.append((t, TextDelta(chunk)))
        t += every_s
    if stop is not None:
        events.append((t, Stop(stop)))
    if usage is not None:
        events.append((t, UsageEvent(usage)))
    return events


def reasoning(at: float, chars: int) -> tuple[float, StreamEvent]:
    return (at, ReasoningDelta(chars))


def error_at(at: float, kind: ErrorKind, code: str, retry_after_s: float | None = None) -> tuple[float, StreamEvent]:
    return (at, ErrorEvent(failure(kind, code, retry_after_s)))


def failure(kind: ErrorKind, code: str, retry_after_s: float | None = None) -> ClassifiedError:
    """A classified failure (`cause` model_access_denied for tenant_access)."""
    cause: TenantAccessCause | None = "model_access_denied" if kind is ErrorKind.TENANT_ACCESS else None
    return ClassifiedError(kind, "scripted failure", code, retry_after_s, cause)


Scripts = Mapping[str, Sequence[Call]]
