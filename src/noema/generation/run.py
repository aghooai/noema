"""`StreamingProfileRun`: one generation profile run (spec-02 §10, spec-04 §3, §4.1, §6; IMPL-011, IMPL-016).

Attempt-level events go to one unbounded in-memory stream (`events()`, a single consumer: the orchestrator).
Answer text is held in each attempt's buffer until the commit point (`commit()`, or the first text when armed /
hold=False), then flushed as `generation.delta`s and streamed live. The pre-commit failure walk lives in
`noema.generation.walk` (mixed in); the per-event handlers are sync, so every state change is atomic.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final, Literal

import anyio
import anyio.abc
import anyio.lowlevel

from noema.config.models import CHARS_PER_TOKEN_V1, TOKEN_ESTIMATOR_V1, GenerationProfileCfg
from noema.core.clock import Clock, SystemClock
from noema.core.errors import ClassifiedError, ErrorCode, ErrorKind, NoemaError
from noema.core.ids import IdGen
from noema.core.protocols.adapters import (
    AdapterError,
    AdapterRegistry,
    ErrorEvent,
    MessageStart,
    ModelRequest,
    ReasoningDelta,
    Stop,
    StopReason,
    StreamEvent,
    StreamTimeouts,
    TextDelta,
    UsageEvent,
)
from noema.core.protocols.generation import (
    AttemptBudget,
    AttemptRecord,
    AttemptSpec,
    GenEvent,
    PromptBuilder,
    TurnContext,
)
from noema.core.protocols.ops import CostMeter, HealthRegistry, RetryBudget, TokenEstimator
from noema.core.protocols.telemetry import Span, Telemetry
from noema.core.values import Cost, UsageRecord

from .attempt import Attempt, CancelReason
from .events import EventQueue
from .fsm import In, St, step
from .policy import FINISH, estimate_usage, usage_wire, walk_error
from .spans import annotate_attempt

log = logging.getLogger("noema.generation")

CommitReason = Literal["spec_agrees", "holdback_deadline", "gate"]
RunCancelReason = Literal["spec_changed", "client_abort", "deadline"]
_CANCEL_INPUT: Final[dict[str, In]] = {
    "spec_changed": In.SPEC_CHANGED,
    "client_abort": In.CLIENT_ABORT,
    "deadline": In.DEADLINE,
}
STREAM_FAILED: Final = "The answer stream failed."
NO_ROUTE: Final = "No route could answer."
# The breaker's TTFT samples come from comparable attempts only: a longer prompt (prefill) or reasoning before the
# first text (effort) is latency the tenant shapes, and the service breaker is shared by every tenant (Wave E)
TTFT_SIGNAL_MAX_INPUT_TOKENS: Final = 8192


def _wall(att: Attempt, mono: float | None) -> datetime | None:
    """A monotonic instant of the attempt on the wall clock of its `started_at`."""
    return None if mono is None else att.started_at + timedelta(seconds=mono - att.started_mono)


@dataclass(frozen=True, slots=True)
class GenerationDeps:
    """What a run needs from the process (all injected by the composition root)."""

    clock: Clock
    ids: IdGen
    adapters: AdapterRegistry
    health: HealthRegistry
    retry_budget: RetryBudget
    estimator: TokenEstimator
    cost: CostMeter
    telemetry: Telemetry
    rng: random.Random


class RunCore:
    """State, the public `ProfileRun` surface and the attempt state transitions."""

    def __init__(
        self,
        deps: GenerationDeps,
        ctx: TurnContext,
        spec: AttemptSpec,
        profile: GenerationProfileCfg,
        prompt: PromptBuilder,
        *,
        hold: bool,
        speculative: bool,
        auto_commit: Literal["spec_agrees", "gate"],
        failure_budget: AttemptBudget,
    ) -> None:
        self._deps = deps
        self._clock = deps.clock
        self._ctx = ctx
        self._spec = spec
        self._signature = spec.signature()
        self._profile = profile
        self._prompt = prompt
        self._speculative = speculative
        self._strategy = "gate" if auto_commit == "gate" else "holdback"
        self._budget = failure_budget
        self._armed: CommitReason | None = None if hold else auto_commit
        self._queue = EventQueue()
        self._attempts: list[Attempt] = []
        self._committed: Attempt | None = None
        self._cancelled: RunCancelReason | None = None
        self._expired = False
        self._current: Attempt | None = None  # the attempt whose own task is handling an event right now
        self._first_text_at: float | None = None
        self._first_text = anyio.Event()
        self._ended = anyio.Event()
        self._changed = anyio.Event()
        self._upstream = False
        self._last_error: ClassifiedError | None = None
        self._tenant_access = False
        self._failure_attempts = 0
        self._walk_scope = anyio.CancelScope()
        self._watch_scope: anyio.CancelScope | None = (
            None  # the deadline watcher task's (clocks other than SystemClock)
        )
        self._deadline_timer: asyncio.TimerHandle | None = None  # SystemClock: the loop's timer instead of a task
        self._tg: anyio.abc.TaskGroup | None = None
        self._overrun = ctx.snapshot.platform.cost.cancel_overrun_tokens

    # --- ProfileRun -----------------------------------------------------------------------------------------------
    @property
    def spec(self) -> AttemptSpec:
        return self._spec

    @property
    def upstream_started(self) -> bool:
        """`ProfileRun.upstream_started`: True once any attempt of the run received MessageStart, reasoning, text or
        a stop (usage and error events do not count): the orchestrator's `decision.completed.arrived`
        before_generation / during_holdback rule (spec-04 §3)."""
        return self._upstream

    @property
    def attempts(self) -> tuple[Attempt, ...]:
        """The attempts started so far (read-only inspection, start order)."""
        return tuple(self._attempts)

    def events(self) -> AsyncIterator[GenEvent]:
        """Attempt-level events in order (single consumer); ends after the last attempt's terminal event."""
        return self._queue

    async def first_text(self) -> float | None:
        await self._first_text.wait()
        return self._first_text_at

    async def commit(self, reason: CommitReason) -> None:
        """The commit point (T6/T8): commits the attempt in B with the earliest text. With none in B the run is armed
        and its next first text commits with `reason` (T7). That includes a hold cap (`holdback_deadline`) whose
        first text came from an attempt that failed since (ruling R2, IMPL-012 amendment, spec-04 §3): the fallback
        commits at its own first text with `holdback_deadline`; the TTFT bound already fired."""
        await anyio.lowlevel.checkpoint()
        if self._ended.is_set() or self._committed is not None or self._stopped:
            return
        ready = [a for a in self._attempts if a.state is St.B]
        if ready:
            self._commit(min(ready, key=lambda a: (a.first_text_at or 0.0, a.index)), reason, In.COMMIT)
        else:
            self._armed = reason  # commits at the first text (T7)

    async def cancel(self, reason: RunCancelReason) -> None:
        await anyio.lowlevel.checkpoint()
        if self._ended.is_set() or self._stopped:
            return
        if reason == "spec_changed" and self._committed is not None:
            return  # T9 covers P/S/B only; after a commit no other attempt is live and the answer is kept
        self._cancelled = reason
        self._walk_scope.cancel()
        inp = _CANCEL_INPUT[reason]
        for att in self._attempts:
            self._cancel_attempt(att, inp)
        self._notify()

    async def result(self) -> tuple[AttemptRecord, ...]:
        await self._ended.wait()
        return tuple(a.record for a in self._attempts if a.record is not None)

    # --- small helpers --------------------------------------------------------------------------------------------
    @property
    def _stopped(self) -> bool:
        return self._cancelled is not None or self._expired

    def _notify(self) -> None:
        """Wake whoever waits for a run-level change (the race wait of the walk); called on commits, cancels and
        terminal transitions, never per delta."""
        changed = self._changed
        if not changed.is_set():
            changed.set()
        self._changed = anyio.Event()

    def _emit(self, event: GenEvent) -> None:
        self._queue.put(event)

    def _delta(self, att: Attempt, text: str) -> None:
        self._emit({"type": "generation.delta", "attempt_id": att.attempt_id, "seq": att.seq, "text": text})
        att.seq += 1

    def _now(self) -> float:
        return self._clock.monotonic()

    # --- adapter stream events (sync; the attempt's own task) ------------------------------------------------------
    def _on_text(self, att: Attempt, text: str) -> None:
        state = att.state
        att.text_chars += len(text)
        if state is St.C:  # T14
            self._delta(att, text)
            return
        att.buffer.append(text)
        if state is St.B:  # T5
            return
        self._note_first_text(att)
        if self._commit_ready():
            self._commit(att, self._armed or "spec_agrees", In.TEXT_ARMED)  # T7
        else:
            att.state = step(state, In.TEXT)  # T4

    def _on_stop(self, att: Attempt, reason: StopReason) -> None:
        att.stop = reason
        state = att.state
        if state in (St.P, St.S) and reason == "content_filter" and att.text_chars == 0:
            att.state = step(state, In.FILTER_STOP)  # T4: an empty committable answer (04:L114)
            self._note_first_text(att)
            if self._commit_ready():
                self._commit(att, self._armed or "spec_agrees", In.COMMIT)
        elif state in (St.B, St.C):
            att.state = step(state, In.HOLD)  # T5 / held until the stream ends (T15)

    def _on_end(self, att: Attempt) -> None:
        """The adapter stream ended without an ErrorEvent (ignored once the attempt is terminal, see `_on_event`)."""
        if att.is_terminal:
            return
        att.stream_ended = True
        state = att.state
        if att.stop is None:
            self._fail(att, ClassifiedError(ErrorKind.RETRYABLE, "the stream ended without a stop", "no_stop"))
        elif state is St.C:
            self._complete(att)  # T15
        elif state is St.B:
            att.state = step(state, In.HOLD)  # a complete answer, still held for the commit point
        else:  # P / S: a text-less stop other than content_filter is a pre-commit failure (T10, 06 §7)
            code = "reasoning_only" if att.stop == "max_tokens" else "empty_text"
            self._fail(att, ClassifiedError(ErrorKind.RETRYABLE, "the attempt produced no answer text", code))

    def _note_first_text(self, att: Attempt) -> None:
        now = self._now()
        att.first_text_at = now
        att.reasoned_before_text = att.reasoning_chars > 0
        self._deps.telemetry.instruments.observe(
            "aghoo_first_answer_seconds",
            now - att.started_mono,
            profile=att.route_id,
            model=att.model_id,
            strategy=self._strategy,
        )
        if self._first_text_at is None:
            self._first_text_at = now
            self._first_text.set()

    def _commit_ready(self) -> bool:
        return self._armed is not None and self._committed is None and not self._stopped

    def _commit(self, att: Attempt, reason: CommitReason, inp: In) -> None:
        """T6 / T7 / T8: committed, the losers' lost_race, the buffered deltas, then the held completion."""
        att.state = step(att.state, inp)
        self._committed = att
        self._emit({"type": "generation.committed", "attempt_id": att.attempt_id, "reason": reason})
        for other in self._attempts:
            if other is not att:
                self._cancel_attempt(other, In.LOST_RACE)  # T13
        buffered, att.buffer = att.buffer, []
        for text in buffered:
            self._delta(att, text)
        if att.stream_ended:
            self._complete(att)
        self._notify()

    # --- failures, cancels and terminal transitions ---------------------------------------------------------------
    def _fail(self, att: Attempt, err: ClassifiedError) -> None:
        """ErrorEvent, adapter error, timeout or a text-less stop: T10 before commit, T16/T17 after; an adapter
        `timeout:deadline` (or any failure once ctx.deadline passed) is T19 for the whole run (no frame, no walk)."""
        if att.is_terminal:
            return
        if err.provider_code == "timeout:deadline" or self._ctx.deadline.expired():
            self._expire()
            return
        if att.stop is not None and att.state in (St.B, St.C):
            # the answer was complete: a trailing error (e.g. instead of the usage frame) ends it like a stream end
            log.info("stream error after the stop", extra={"attempt_id": att.attempt_id})
            self._on_end(att)
            return
        att.error = err
        if att.state is St.C:
            stalled = err.provider_code == "timeout:idle"
            code = ErrorCode.UPSTREAM_STALLED if stalled else walk_error(err.kind)
            self._emit(NoemaError(code, STREAM_FAILED).sse_event())
            self._terminate(att, In.STALL if stalled else In.FAIL, outcome="committed", error_code=code.value)
            return
        self._last_error = err
        self._tenant_access = self._tenant_access or err.kind is ErrorKind.TENANT_ACCESS
        self._emit({"type": "generation.cancelled", "attempt_id": att.attempt_id, "reason": "failed"})
        self._terminate(att, In.FAIL, outcome="failed", cancel_reason="failed", error_code=walk_error(err.kind).value)
        log.info(
            "generation attempt failed",
            extra={"attempt_id": att.attempt_id, "model": att.model_id, "code": err.provider_code},
        )

    def _cancel_attempt(self, att: Attempt, inp: In) -> None:
        """T9 / T13 / T18 / T19. The committed attempt is only stopped by client_abort (frame) or the deadline (no
        frame), and keeps outcome committed with finish_reason cancelled (IMPL-011)."""
        if att.is_terminal:
            return
        if att.state is St.C:
            if inp is In.CLIENT_ABORT:
                self._emit({"type": "generation.cancelled", "attempt_id": att.attempt_id, "reason": "client_abort"})
            elif inp is not In.DEADLINE:
                return
            self._terminate(att, inp, outcome="committed", finish_reason="cancelled")
            return
        reason: CancelReason = inp.value  # type: ignore[assignment]  # the four cancel inputs are CancelReasons
        if inp is not In.DEADLINE:
            self._emit({"type": "generation.cancelled", "attempt_id": att.attempt_id, "reason": reason})
        self._terminate(att, inp, outcome="cancelled", cancel_reason=reason)

    def _complete(self, att: Attempt) -> None:
        """T15: generation.completed{finish_reason, usage} once the stream ended after its Stop."""
        assert att.stop is not None
        finish = FINISH[att.stop]
        record = self._terminate(att, In.COMPLETE, outcome="committed", finish_reason=finish, signal=False)
        self._emit(
            {
                "type": "generation.completed",
                "attempt_id": att.attempt_id,
                "finish_reason": finish,
                "usage": usage_wire(record.usage, record.cost),
            }
        )
        self._signal_terminal(att)

    def _expire(self) -> None:
        """ctx.deadline (or the turn left the run with live attempts): every live attempt ends by T19."""
        if self._ended.is_set() or self._expired:
            return
        self._expired = True
        self._walk_scope.cancel()
        for att in self._attempts:
            self._cancel_attempt(att, In.DEADLINE)
        self._notify()

    def _terminate(
        self,
        att: Attempt,
        inp: In,
        *,
        outcome: Literal["committed", "cancelled", "failed"],
        cancel_reason: CancelReason | None = None,
        finish_reason: Literal["stop", "length", "content_filter", "cancelled"] | None = None,
        error_code: str | None = None,
        signal: bool = True,
    ) -> AttemptRecord:
        """Move `att` to its terminal state, fix its record (usage: provider, else the IMPL-020 estimate) and
        report it to the breaker and the waste metric."""
        att.state = step(att.state, inp)
        att.outcome, att.cancel_reason, att.finish_reason = outcome, cancel_reason, finish_reason
        att.error_code = error_code
        att.ended_at = self._clock.now()
        usage = self._usage_of(att)
        record = att.record = AttemptRecord(
            attempt_id=att.attempt_id,
            route_id=att.route_id,
            model=att.model_id,
            outcome=outcome,
            cancel_reason=cancel_reason,
            usage=usage,
            cost=self._price(att, usage),
            finish_reason=finish_reason,
            error_code=error_code,
            started_at=att.started_at,
            ended_at=att.ended_at,
            spec_signature=self._signature,
            first_text_at=_wall(att, att.first_text_at),
        )
        if outcome != "committed" and usage.output_tokens > 0:
            self._deps.telemetry.instruments.add(
                "aghoo_wasted_output_tokens_total",
                usage.output_tokens,
                profile=att.route_id,
                reason=cancel_reason or "failed",
            )
        self._record_health(att)
        if signal:
            self._signal_terminal(att)
        return record

    def _signal_terminal(self, att: Attempt) -> None:
        att.terminal.set()
        if att is not self._current:
            att.scope.cancel()  # leave the adapter stream context (aclose, no draining)
        self._notify()

    def _usage_of(self, att: Attempt) -> UsageRecord:
        if att.usage is not None:
            return att.usage
        cut = att.opened and att.stop is None  # tokens may still be generated after we stopped reading
        return estimate_usage(
            input_tokens=att.input_tokens,
            text_chars=att.text_chars,
            reasoning_chars=att.reasoning_chars,
            chars_per_token=TOKEN_ESTIMATOR_V1.get(att.alias, (CHARS_PER_TOKEN_V1, 0))[0],
            overrun_tokens=self._overrun if cut else 0,
        )

    def _price(self, att: Attempt, usage: UsageRecord) -> Cost:
        """CostMeter.price with the turn's pinned snapshot (IMPL-019) and the tenant's home region (spec-11 §2);
        estimated usage always gives an estimated cost (IMPL-020)."""
        try:
            cost = self._deps.cost.price(att.alias, self._ctx.tenant.home_region, usage, snapshot=self._ctx.snapshot)
        except NoemaError:  # a meter that cannot price the alias: priced as TBD
            log.warning("no price for the attempt's model", extra={"attempt_id": att.attempt_id})
            return Cost(usd=Decimal(0), estimated=True)
        if usage.tokens_estimated and not cost.estimated:
            return Cost(usd=cost.usd, estimated=True)
        return cost

    def _record_health(self, att: Attempt) -> None:
        """Breaker outcome per attempt: ok once the provider answered, throttled, timeout or error. A text-less stop
        (reasoning_only, empty_text) is an error (fault B1: the model keeps answering without text, so the breaker
        lets later turns skip it), except a reasoning-only stop under a cap the request lowered (`client_capped`:
        tenant-shaped, like the TTFT exclusions below). Tenant-access and invalid-request failures and attempts
        cancelled before any upstream event are not health signals."""
        err = att.error
        outcome: Literal["ok", "error", "throttled", "timeout"]
        if err is not None and err.provider_code in ("reasoning_only", "empty_text"):
            if err.provider_code == "reasoning_only" and att.client_capped:
                return
            outcome = "error"
        elif err is None:
            if not att.upstream:
                return
            outcome = "ok"
        elif err.kind is ErrorKind.THROTTLED:
            outcome = "throttled"
        elif err.kind in (ErrorKind.RETRYABLE, ErrorKind.FATAL):
            outcome = "timeout" if (err.provider_code or "").startswith("timeout:") else "error"
        else:
            return
        ttft = None if att.first_text_at is None else round((att.first_text_at - att.started_mono) * 1000)
        if att.input_tokens > TTFT_SIGNAL_MAX_INPUT_TOKENS or att.reasoned_before_text:
            ttft = None  # tenant-shaped (prefill, effort): no sample for the shared breaker's p95-TTFT rule
        tenant = self._ctx.tenant
        self._deps.health.record(att.model_id, tenant.home_region, tenant.aws_account_id, outcome, ttft)

    # --- the attempt task -----------------------------------------------------------------------------------------
    async def _run_attempt(
        self, att: Attempt, req: ModelRequest, timeouts: StreamTimeouts, attrs: dict[str, str | int | float | bool]
    ) -> None:
        """One adapter stream, inside the attempt's cancel scope and its `generation.attempt` span (the span lasts
        until the attempt is terminal, a held answer included)."""
        with att.scope, self._deps.telemetry.span("generation.attempt", **attrs) as span:
            try:
                await self._stream(att, req, timeouts)
                if not att.is_terminal:
                    await att.terminal.wait()
            finally:
                self._annotate(span, att)

    async def _stream(self, att: Attempt, req: ModelRequest, timeouts: StreamTimeouts) -> None:
        handling = False  # True while our own handler runs: its exceptions are bugs, never adapter failures
        adapter = None
        try:
            adapter = self._deps.adapters.for_model(att.alias)
            async with adapter.stream(
                req, tenant=self._ctx.tenant, timeouts=timeouts, deadline=self._ctx.deadline
            ) as stream:
                att.opened = True
                async for ev in stream:
                    handling, self._current = True, att
                    self._on_event(att, ev)
                    handling, self._current = False, None
                    if att.is_terminal:
                        break
                else:
                    handling, self._current = True, att
                    self._on_end(att)
                    handling, self._current = False, None
        except AdapterError as exc:
            if handling:
                raise
            self._own_failure(att, exc.error)
        except Exception as exc:
            if handling:
                raise
            if adapter is None:
                err = ClassifiedError(ErrorKind.FATAL, "no adapter for the model", type(exc).__name__)
            else:
                err = adapter.classify_error(exc)
            log.error("unclassified adapter exception", exc_info=True, extra={"attempt_id": att.attempt_id})
            self._own_failure(att, err)

    def _own_failure(self, att: Attempt, err: ClassifiedError) -> None:
        self._current = att
        self._fail(att, err)
        self._current = None

    def _on_event(self, att: Attempt, ev: StreamEvent) -> None:
        """One adapter event of `att` (its own task). An event of an attempt that is already terminal is dropped:
        another task ended it (lost_race, a cancel, ctx.deadline) while this event was already scheduled, and anyio
        does not deliver a cancel to a task whose wake-up is pending."""
        if type(ev) is TextDelta and att.state is St.C:  # the per-delta hot path (T14)
            text = ev.text
            if text:
                att.text_chars += len(text)
                self._delta(att, text)
            return
        if att.is_terminal:
            return
        if type(ev) is TextDelta:
            text = ev.text
            self._mark_upstream(att)
            if text:
                self._on_text(att, text)
            elif att.state is St.P:
                att.state = step(St.P, In.UPSTREAM)
        elif type(ev) is ReasoningDelta:
            att.reasoning_chars += ev.chars
            self._mark_upstream(att)
            if att.state is St.P:
                att.state = step(St.P, In.UPSTREAM)  # T3
        elif type(ev) is MessageStart:
            self._mark_upstream(att)
            if att.state is St.P:
                att.state = step(St.P, In.UPSTREAM)  # T3
        elif type(ev) is Stop:
            self._mark_upstream(att)
            self._on_stop(att, ev.reason)
        elif type(ev) is UsageEvent:
            att.usage = ev.usage
        elif type(ev) is ErrorEvent:
            self._fail(att, ev.error)

    def _mark_upstream(self, att: Attempt) -> None:
        if not att.upstream:
            att.upstream = True
            self._upstream = True

    def _annotate(self, span: Span, att: Attempt) -> None:
        annotate_attempt(span, att)

    # --- lifecycle (driven by the engine's context manager) -------------------------------------------------------
    def _arm_deadline(self, tg: anyio.abc.TaskGroup) -> None:
        """T19 at ctx.deadline even when no adapter call is open (a held answer, a backoff sleep, a prompt build): on
        `SystemClock` the event loop's timer calls `_expire` (E3: no watcher task per run; `_expire` is sync), on
        any other clock a task sleeps on it."""
        delay = max(0.0, self._ctx.deadline.at - self._now())
        if type(self._clock) is SystemClock:
            self._deadline_timer = asyncio.get_running_loop().call_later(delay, self._expire)
        else:
            self._watch_scope = anyio.CancelScope()
            tg.start_soon(self._watch_deadline, self._watch_scope, delay)

    async def _watch_deadline(self, scope: anyio.CancelScope, delay: float) -> None:
        with scope:
            await self._clock.sleep(delay)
            self._expire()

    def _finish(self) -> None:
        """Every attempt is terminal and the walk is over: resolve first_text (None if no text) and end events()."""
        if self._watch_scope is not None:
            self._watch_scope.cancel()
        if self._deadline_timer is not None:
            self._deadline_timer.cancel()
        if not self._first_text.is_set():
            self._first_text.set()
        self._ended.set()
        self._queue.close()
        self._notify()

    async def close(self) -> None:
        """The engine's `__aexit__`. Attempts still live here are stuck (the turn left without awaiting
        `result()`): they end as cancelled(deadline) with estimated usage and count in aghoo_stuck_attempts_total
        (spec-11 §6). Waits until the run has ended."""
        if self._ended.is_set():
            return
        live = sum(1 for a in self._attempts if not a.is_terminal)
        if live:
            self._deps.telemetry.instruments.add("aghoo_stuck_attempts_total", live)
            log.warning("generation attempts left live at turn exit", extra={"turn_id": self._ctx.turn_id})
        self._expire()
        await self._ended.wait()
