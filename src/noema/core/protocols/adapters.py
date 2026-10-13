"""Model adapters (spec-02 §4; the wire mapping §4.2 and error classification §4.3 are frozen; IMPL-050,
IMPL-076).

Adapters never parse answer distributions (the decision elicitation layer does) and fetch tenant credentials
themselves on every call (`CredentialBroker.get` + `assert_creds_for`, before any byte is sent).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from noema.core.clock import Deadline
from noema.core.errors import ClassifiedError
from noema.core.principal import TenantRef
from noema.core.values import CallKind, Effort, Message, Modality, UsageRecord

if TYPE_CHECKING:
    from noema.core.protocols.config import ConfigSnapshot

StopReason = Literal["end_turn", "max_tokens", "stop_sequence", "content_filter", "other"]


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """One provider call. `temperature` is set only if the model's `controls.temperature` is true (the caller drops
    it otherwise). Decision, verification and labeling member calls use `retry="none"` (IMPL-050). `snapshot`: the
    caller's pinned config snapshot (a turn or decision pins one at ingress, spec-02 §2); the adapter resolves
    `models.<model_alias>` from it, so a config activation mid-turn cannot re-resolve an attempt; None = the
    registry's current snapshot."""

    model_alias: str
    system: tuple[str, ...]
    messages: tuple[Message, ...]
    max_tokens: int
    call_kind: CallKind
    retry: Literal["none", "default"]
    effort: Effort | None = None
    temperature: float | None = None
    logprobs: bool = False
    top_logprobs: int | None = None
    stop: tuple[str, ...] = ()
    snapshot: ConfigSnapshot | None = None


@dataclass(frozen=True, slots=True)
class TokenLogprob:
    """One output token with its log-probability and the top alternatives `(token, logprob)`."""

    token: str
    logprob: float
    top: tuple[tuple[str, float], ...]


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """A completed non-streaming call. `text` concatenates the answer text blocks (reasoning excluded); `refused`
    is a provider-signalled refusal only (IMPL-053)."""

    text: str
    logprobs: tuple[TokenLogprob, ...] | None
    refused: bool
    stop_reason: StopReason
    usage: UsageRecord
    latency_ms: int
    region: str
    model_revision: str | None = None


@dataclass(frozen=True, slots=True)
class MessageStart:
    """The provider opened the answer; `at` is `Clock.monotonic()`."""

    at: float


@dataclass(frozen=True, slots=True)
class ReasoningDelta:
    """Reasoning output, counted and never forwarded."""

    chars: int


@dataclass(frozen=True, slots=True)
class TextDelta:
    """Answer text."""

    text: str


@dataclass(frozen=True, slots=True)
class UsageEvent:
    """Provider usage (after `Stop` on Converse)."""

    usage: UsageRecord


@dataclass(frozen=True, slots=True)
class Stop:
    """The provider finished the answer."""

    reason: StopReason


@dataclass(frozen=True, slots=True)
class ErrorEvent:
    """A failure after the stream opened; always the last event."""

    error: ClassifiedError


StreamEvent = MessageStart | ReasoningDelta | TextDelta | UsageEvent | Stop | ErrorEvent


@dataclass(frozen=True, slots=True)
class StreamTimeouts:
    """Per-attempt stream timeouts in seconds (ROUTE_TIMEOUTS_V1, spec-05 §1.4)."""

    connect_s: float
    first_event_s: float
    first_answer_s: float
    idle_s: float


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    """What a model alias accepts. `context_window` is `models.<alias>.context_window` or DEFAULT_CONTEXT_WINDOW
    (IMPL-084); `chars_per_token` is 4.0 for every Phase 1 model (a code constant, IMPL-029c)."""

    modalities: frozenset[Modality]
    temperature: bool
    effort: frozenset[Effort]
    logprobs: bool
    top_logprobs_max: int | None
    structured_output: bool
    reasoning: bool
    max_output_tokens_cap: int | None
    context_window: int
    chars_per_token: float


class AdapterError(Exception):
    """A classified provider failure raised by `Adapter.complete` or `Adapter.stream.__aenter__`."""

    def __init__(self, error: ClassifiedError) -> None:
        super().__init__(error.message)
        self.error: ClassifiedError = error


class Adapter(Protocol):
    """One provider API (`bedrock-converse` or `bedrock-mantle-chat`)."""

    def capabilities(self, alias: str) -> ModelCapabilities:
        """Sync: the current snapshot's `models.<alias>` capabilities."""
        ...

    async def complete(self, req: ModelRequest, *, tenant: TenantRef, deadline: Deadline) -> ModelResponse:
        """Non-streaming call. Fetches and asserts the tenant's credentials before any byte (IMPL-076).

        The deadline bounds the whole call, connect included; expiry raises
        `AdapterError(retryable, provider_code="timeout:deadline")`. Every failure raises `AdapterError`.
        Cancellation closes the HTTP request (the provider may still bill it).
        """
        ...

    def stream(
        self, req: ModelRequest, *, tenant: TenantRef, timeouts: StreamTimeouts, deadline: Deadline
    ) -> AbstractAsyncContextManager[AsyncIterator[StreamEvent]]:
        """Streaming call.

        `__aenter__` fetches and asserts credentials and opens the request; a failure there raises `AdapterError`.
        Afterwards every failure (provider exception event, timeouts, malformed frames) is yielded as one
        `ErrorEvent`, the last event. A successful stream ends with `Stop`, then `UsageEvent` if the provider sent
        usage. Timeouts (each capped by `deadline`): connect_s (before `__aenter__` returns), first_event_s,
        first_answer_s (first `TextDelta`), idle_s (between events); expiry yields
        `ErrorEvent(retryable, provider_code="timeout:<name>")`. Exiting the context cancels (acloses the provider
        stream, no draining). `req.retry == "default"`: one in-adapter retry on a connect/DNS/TLS error before any
        response byte (RetryBudget-limited); `"none"`: never retried (IMPL-016, IMPL-050).
        """
        ...

    def classify_error(self, exc: BaseException) -> ClassifiedError:
        """Sync: map a provider exception to a `ClassifiedError` (spec-02 §4.3)."""
        ...


class AdapterRegistry(Protocol):
    """Finds the adapter of a model alias."""

    def for_model(self, alias: str) -> Adapter:
        """Sync: the adapter for `models.<alias>.api`."""
        ...
