"""Generation attempts and profile runs (spec-02 §10, spec-04; IMPL-011, IMPL-014-IMPL-016, IMPL-086)."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

import orjson

from noema.core.clock import Deadline
from noema.core.principal import Principal, TenantRef
from noema.core.protocols.config import ConfigSnapshot
from noema.core.protocols.ops import Reservation
from noema.core.values import Cost, Degradation, Effort, JsonValue, Message, UsageRecord


@dataclass(frozen=True, slots=True)
class AttemptSpec:
    """What an attempt generates with; equality is over the 4-tuple (IMPL-014, IMPL-015).
    `conditioning_signature` is "none" if there is no conditioning block."""

    generation_profile: str
    effort: Effort | None
    max_output_tokens: int | None
    conditioning_signature: str

    def signature(self) -> str:
        """`"sha256:" + sha256(orjson([profile, effort, max_output_tokens, conditioning_signature]))` (hex):
        attempt.spec_signature and the aghoo.attempt.spec_signature span attribute."""
        body = orjson.dumps([self.generation_profile, self.effort, self.max_output_tokens, self.conditioning_signature])
        return "sha256:" + hashlib.sha256(body).hexdigest()


@dataclass(frozen=True, slots=True)
class Conditioning:
    """Rendered by turns/conditioning.py (spec-04 §5.2): the wire Conditioning fields plus the prompt block."""

    answer_mode: str
    signature: str
    summary: str
    block: str


@dataclass(frozen=True, slots=True)
class AssembledPrompt:
    """One attempt's prompt for one model."""

    system: tuple[str, ...]
    messages: tuple[Message, ...]
    conditioning: Conditioning | None
    degradations: tuple[Degradation, ...]


class PromptBuilder(Protocol):
    """Implemented by turns/prompt; called by generation at every attempt start."""

    async def build(self, alias: str) -> AssembledPrompt:
        """Media negotiated for this model (spec-07 §4); standard spotlighting always, strict if its trigger is
        final by now (IMPL-086). Never raises for media problems."""
        ...


class AttemptBudget:
    """The turn-wide failure budget (IMPL-016): at most `failures` failure attempts (fallbacks) per turn."""

    __slots__ = ("remaining",)

    def __init__(self, failures: int = 2) -> None:
        if failures < 0:
            raise ValueError("failures must be >= 0")
        self.remaining: int = failures

    def try_take(self) -> bool:
        """Sync: take one unit; False when exhausted."""
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


@dataclass(frozen=True, slots=True)
class TurnContext:
    """Per-turn context handed to the generation engine."""

    principal: Principal
    tenant: TenantRef
    snapshot: ConfigSnapshot
    deadline: Deadline
    reservation: Reservation
    turn_id: str
    session_id: str | None
    trace_id: str


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """One finished attempt. `route_id` = the generation profile actually used; `model` = the Bedrock id
    (TurnObject.attempts[].model); usage is estimated for cancelled attempts (IMPL-020). `first_text_at` (C2b):
    when this attempt received its first `TextDelta` (or a text-less `Stop{content_filter}`), on the clock of
    `started_at`; None if it never did. The orchestrator's `timings.holdback_ms` is commit - this (spec-04 §10)."""

    attempt_id: str
    route_id: str
    model: str
    outcome: Literal["committed", "cancelled", "failed"]
    cancel_reason: Literal["spec_changed", "lost_race", "client_abort", "failed", "deadline"] | None
    usage: UsageRecord
    cost: Cost
    finish_reason: Literal["stop", "length", "content_filter", "cancelled"] | None
    error_code: str | None
    started_at: datetime
    ended_at: datetime
    spec_signature: str
    first_text_at: datetime | None = None


# Exactly the openapi payloads of generation.started / delta / committed / cancelled / completed and error; the
# orchestrator appends them as given.
type GenEvent = Mapping[str, JsonValue]


class ProfileRun(Protocol):
    """One generation profile run; its members are attempts (IMPL-011)."""

    @property
    def spec(self) -> AttemptSpec: ...

    @property
    def upstream_started(self) -> bool:
        """True once any attempt of the run received an upstream event (MessageStart, reasoning, text or a stop;
        usage and error events do not count); sync, in-memory. The orchestrator's `decision.completed.arrived`
        rule: `before_generation` while False, else `during_holdback` (spec-04 §3)."""
        ...

    def events(self) -> AsyncIterator[GenEvent]:
        """Attempt-level events in order; deltas only after commit; ends after the last attempt's terminal event.
        No generation.cancelled for reason deadline (the turn emits error{deadline_exceeded})."""
        ...

    async def first_text(self) -> float | None:
        """Monotonic time of the first buffered answer text, or of a text-less Stop{content_filter} (an empty
        committable answer, surfaced as finish_reason content_filter, never rerouted); None if the run ended with
        neither."""
        ...

    async def commit(self, reason: Literal["spec_agrees", "holdback_deadline", "gate"]) -> None:
        """Flushes the buffer; idempotent; a no-op after the run ended."""
        ...

    async def cancel(self, reason: Literal["spec_changed", "client_abort", "deadline"]) -> None:
        """Idempotent; committed text is kept."""
        ...

    async def result(self) -> tuple[AttemptRecord, ...]:
        """Waits until every attempt is terminal; started_at order."""
        ...


class GenerationEngine(Protocol):
    def run(
        self,
        ctx: TurnContext,
        spec: AttemptSpec,
        prompt: PromptBuilder,
        *,
        hold: bool,
        speculative: bool,
        auto_commit: Literal["spec_agrees", "gate"] = "spec_agrees",
        failure_budget: AttemptBudget,
    ) -> AbstractAsyncContextManager[ProfileRun]:
        """hold=True buffers until commit(); hold=False commits at the first answer text with reason auto_commit
        (spec-04 §3). `speculative` = generation.started.speculative (A1). `__aenter__` raises only
        `NoemaError(tenant_access_misconfigured)`; `__aexit__` cancels non-terminal attempts and awaits them. The
        orchestrator appends route.confirmed before the run's generation.committed event (spec-04 §4)."""
        ...
