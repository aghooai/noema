"""One generation attempt: one model call (IMPL-011) with its state, holdback buffer and counters."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

import anyio

from noema.core.errors import ClassifiedError
from noema.core.protocols.adapters import StopReason
from noema.core.protocols.generation import AttemptRecord
from noema.core.values import Effort, UsageRecord

from .fsm import TERMINAL, St

CancelReason = Literal["spec_changed", "lost_race", "client_abort", "failed", "deadline"]
Outcome = Literal["committed", "cancelled", "failed"]
AttemptFinish = Literal["stop", "length", "content_filter", "cancelled"]


class Attempt:
    """Mutable per-attempt state, owned by its profile run. `buffer` holds the uncommitted answer text (the
    holdback buffer, flushed at commit); reasoning is only counted."""

    __slots__ = (
        "alias",
        "attempt_id",
        "buffer",
        "cancel_reason",
        "client_capped",
        "effort",
        "ended_at",
        "error",
        "error_code",
        "finish_reason",
        "first_text_at",
        "index",
        "input_tokens",
        "max_tokens",
        "model_id",
        "opened",
        "outcome",
        "reasoned_before_text",
        "reasoning_chars",
        "record",
        "route_id",
        "scope",
        "seq",
        "started_at",
        "started_mono",
        "state",
        "stop",
        "stream_ended",
        "terminal",
        "text_chars",
        "upstream",
        "usage",
    )

    def __init__(
        self,
        *,
        attempt_id: str,
        route_id: str,
        alias: str,
        model_id: str,
        effort: Effort | None,
        max_tokens: int,
        input_tokens: int,
        started_at: datetime,
        started_mono: float,
        index: int,
        client_capped: bool = False,
    ) -> None:
        self.attempt_id = attempt_id
        self.route_id = route_id
        self.alias = alias
        self.model_id = model_id
        self.effort: Effort | None = effort
        self.max_tokens = max_tokens
        self.input_tokens = input_tokens  # TokenEstimator estimate of the attempt's prompt (IMPL-020)
        self.started_at = started_at
        self.started_mono = started_mono
        self.index = index  # start order within the run (race ties go to the lower index)
        self.client_capped = client_capped  # max_tokens below the member's: the request's max_output_tokens lowered it
        self.state = St.P
        self.buffer: list[str] = []
        self.seq = 0
        self.text_chars = 0
        self.reasoning_chars = 0
        self.first_text_at: float | None = None
        self.reasoned_before_text = False  # reasoning deltas came before the first text (set with first_text_at)
        self.stop: StopReason | None = None
        self.stream_ended = False
        self.opened = False  # Adapter.stream.__aenter__ returned
        self.upstream = False  # an upstream event arrived (MessageStart or later)
        self.usage: UsageRecord | None = None  # provider usage (UsageEvent)
        self.error: ClassifiedError | None = None
        self.error_code: str | None = None
        self.outcome: Outcome | None = None
        self.cancel_reason: CancelReason | None = None
        self.finish_reason: AttemptFinish | None = None
        self.ended_at: datetime | None = None
        self.record: AttemptRecord | None = None
        self.scope = anyio.CancelScope()  # exiting it = leaving the adapter stream context (aclose, no draining)
        self.terminal = anyio.Event()

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL
