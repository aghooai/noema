"""`FakeGenerationEngine` / `FakeProfileRun` (spec-02 §10, spec-04 §3, §4.1, §6) and `StaticPromptBuilder`.

The attempt state machine of spec-04 §4.1 over any `AdapterRegistry` (normally the scenario-driven fake adapters):
- attempts start on the first member the breaker admits (T1/T2), build their prompt per attempt
  (`PromptBuilder.build(alias)`), consume the reservation, and emit `generation.started`;
- answer text is buffered until commit (`hold=True` until `commit()`; `hold=False` or an armed run commits at the
  first text with `auto_commit`); a text-less `Stop{content_filter}` is a committable empty answer; other text-less
  stops, `ErrorEvent`s and adapter errors before commit fail the attempt (`generation.cancelled{failed}`, T10);
- the pre-commit failure walk (spec-04 §6): the profile's next member, then `routing.default_profile`'s first
  member, guarded by the turn's `AttemptBudget` (race members don't spend it), `RetryBudget.try_spend` and
  "not tenant_access"; exhausted -> `error{code by ErrorKind}` (IMPL-076); a throttle with no next member gets one
  same-member retry after `retry_after_s` (default 0.25 s) when the deadline allows;
- race: every member starts at once; the first text wins at the commit point; losers -> `cancelled{lost_race}`;
- after commit: deltas live; `idle` timeout -> `error{upstream_stalled}` (T16); other errors -> T17 by kind;
- `cancel(spec_changed | client_abort | deadline)` per T9/T18/T19 (no frame for deadline); an adapter
  `timeout:deadline`, or any failure once `ctx.deadline` passed, ends the whole run by T19 (no frame, no walk; a
  committed attempt keeps its text with finish_reason cancelled). Usage of attempts without provider usage is
  estimated as chars/4 (no IMPL-020 overrun allowance);
- `upstream_started` turns True at an attempt's first MessageStart, reasoning, text or stop (spec-04 §3 `arrived`).
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final, Literal

import anyio
import anyio.abc
import anyio.lowlevel

from noema.config.models import GEN_DEFAULT_MAX_TOKENS, GenMemberCfg
from noema.core.clock import Clock
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
    TextDelta,
    UsageEvent,
)
from noema.core.protocols.generation import (
    AssembledPrompt,
    AttemptBudget,
    AttemptRecord,
    AttemptSpec,
    PromptBuilder,
    TurnContext,
)
from noema.core.protocols.ops import CostMeter, HealthRegistry, RetryBudget
from noema.core.values import Cost, Degradation, Effort, JsonValue, Message, TextBlock, UsageRecord

from .clock import single_errors
from .decision_result import usage_wire

THROTTLE_RETRY_S: Final = 0.25
_FINISH: Final[Mapping[StopReason, Literal["stop", "length", "content_filter"]]] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "content_filter": "content_filter",
    "other": "stop",
}
CancelReason = Literal["spec_changed", "lost_race", "client_abort", "failed", "deadline"]


def walk_error(kind: ErrorKind) -> ErrorCode:
    """The request code of the last pre-commit failure (spec-04 §6 step 4, IMPL-076)."""
    if kind in (ErrorKind.THROTTLED, ErrorKind.RETRYABLE):
        return ErrorCode.ALL_ROUTES_UNAVAILABLE
    if kind is ErrorKind.TENANT_ACCESS:
        return ErrorCode.TENANT_ACCESS_MISCONFIGURED
    return ErrorCode.INTERNAL_ERROR


class StaticPromptBuilder:
    """Implements `PromptBuilder` with one fixed prompt; `builds` records the alias of every call (one per
    attempt start)."""

    def __init__(
        self,
        text: str = "Hello",
        *,
        system: Sequence[str] = ("You are a helpful assistant.",),
        degradations: Sequence[Degradation] = (),
    ) -> None:
        self.prompt = AssembledPrompt(
            system=tuple(system),
            messages=(Message("user", (TextBlock(text),)),),
            conditioning=None,
            degradations=tuple(degradations),
        )
        self.builds: list[str] = []

    async def build(self, alias: str) -> AssembledPrompt:
        await anyio.lowlevel.checkpoint()
        self.builds.append(alias)
        return self.prompt


@dataclass
class _Attempt:
    attempt_id: str
    route_id: str
    alias: str
    model_id: str
    member: GenMemberCfg
    started_at: datetime
    started_mono: float
    effort: Effort | None
    prompt_chars: int = 0
    state: Literal["P", "S", "B", "C", "done", "err", "x", "f"] = "P"
    buffer: list[str] = field(default_factory=list)
    seq: int = 0
    text_chars: int = 0
    reasoning_chars: int = 0
    first_text_at: float | None = None
    stop: StopReason | None = None
    stream_ended: bool = False
    usage: UsageRecord | None = None
    error: ClassifiedError | None = None
    error_code: str | None = None
    outcome: Literal["committed", "cancelled", "failed"] | None = None
    cancel_reason: CancelReason | None = None
    finish_reason: Literal["stop", "length", "content_filter", "cancelled"] | None = None
    ended_at: datetime | None = None
    scope: anyio.CancelScope = field(default_factory=anyio.CancelScope)
    terminal: anyio.Event = field(default_factory=anyio.Event)
    race: bool = False

    @property
    def is_terminal(self) -> bool:
        return self.state in ("done", "err", "x", "f")

    @property
    def committed(self) -> bool:
        return self.state == "C" or self.outcome == "committed"


class FakeProfileRun:
    """Implements `ProfileRun` (see the module docstring)."""

    def __init__(
        self,
        engine: FakeGenerationEngine,
        ctx: TurnContext,
        spec: AttemptSpec,
        prompt: PromptBuilder,
        *,
        hold: bool,
        speculative: bool,
        auto_commit: Literal["spec_agrees", "gate"],
        failure_budget: AttemptBudget,
    ) -> None:
        self._engine = engine
        self._clock: Clock = engine.clock
        self._ctx = ctx
        self._spec = spec
        self._prompt = prompt
        self._hold = hold
        self._speculative = speculative
        self._auto_commit: Literal["spec_agrees", "gate"] = auto_commit
        self._budget = failure_budget
        self._events: list[Mapping[str, JsonValue]] = []
        self._changed = anyio.Event()
        self._attempts: list[_Attempt] = []
        self._ended = anyio.Event()
        self._first_text = anyio.Event()
        self._first_text_at: float | None = None
        self._armed: Literal["spec_agrees", "holdback_deadline", "gate"] | None = None
        self._committed: _Attempt | None = None
        self._cancelled: str | None = None
        self._upstream = False
        self.tg: anyio.abc.TaskGroup | None = None

    @property
    def spec(self) -> AttemptSpec:
        return self._spec

    @property
    def upstream_started(self) -> bool:
        return self._upstream

    @property
    def attempts(self) -> tuple[_Attempt, ...]:
        return tuple(self._attempts)

    # --- ProfileRun ----------------------------------------------------------------------------------------------
    async def events(self) -> AsyncIterator[Mapping[str, JsonValue]]:
        i = 0
        while True:
            while i < len(self._events):
                yield self._events[i]
                i += 1
            if self._ended.is_set():
                return
            changed = self._changed
            await changed.wait()

    async def first_text(self) -> float | None:
        await self._first_text.wait()
        return self._first_text_at

    async def commit(self, reason: Literal["spec_agrees", "holdback_deadline", "gate"]) -> None:
        await anyio.lowlevel.checkpoint()
        if self._ended.is_set() or self._committed is not None or self._cancelled is not None:
            return
        ready = [a for a in self._attempts if not a.is_terminal and a.first_text_at is not None]
        if ready:
            self._do_commit(min(ready, key=lambda a: (a.first_text_at or 0.0, self._attempts.index(a))), reason)
        else:
            self._armed = reason  # commits at the first text (T7)

    async def cancel(self, reason: Literal["spec_changed", "client_abort", "deadline"]) -> None:
        await anyio.lowlevel.checkpoint()
        if self._ended.is_set() or self._cancelled is not None:
            return
        self._cancelled = reason
        for att in self._attempts:
            self._cancel_attempt(att, reason)
        self._signal()

    async def result(self) -> tuple[AttemptRecord, ...]:
        await self._ended.wait()
        return tuple(self._record(a) for a in sorted(self._attempts, key=lambda a: a.started_mono))

    # --- internals ---------------------------------------------------------------------------------------------
    def _signal(self) -> None:
        self._changed.set()
        self._changed = anyio.Event()

    def _emit(self, event: Mapping[str, JsonValue]) -> None:
        self._events.append(event)
        self._signal()

    def _now(self) -> float:
        return self._clock.monotonic()

    def _note_first_text(self, att: _Attempt) -> None:
        if att.first_text_at is None:
            att.first_text_at = self._now()
        if self._first_text_at is None:
            self._first_text_at = att.first_text_at
            self._first_text.set()

    def _can_commit_now(self) -> bool:
        return self._committed is None and self._cancelled is None and (not self._hold or self._armed is not None)

    def _do_commit(self, att: _Attempt, reason: str) -> None:
        self._committed = att
        att.state = "C"
        self._emit({"type": "generation.committed", "attempt_id": att.attempt_id, "reason": reason})
        for other in self._attempts:
            if other is not att:
                self._cancel_attempt(other, "lost_race")
        for text in att.buffer:
            self._delta(att, text)
        att.buffer.clear()
        if att.stream_ended:
            self._complete(att)

    def _delta(self, att: _Attempt, text: str) -> None:
        self._emit({"type": "generation.delta", "attempt_id": att.attempt_id, "seq": att.seq, "text": text})
        att.seq += 1

    def _usage(self, att: _Attempt) -> UsageRecord:
        if att.usage is not None:
            return att.usage
        reasoning = math.ceil(att.reasoning_chars / 4)
        return UsageRecord(
            input_tokens=math.ceil(att.prompt_chars / 4),
            output_tokens=math.ceil(att.text_chars / 4) + reasoning,
            reasoning_tokens=reasoning,
            tokens_estimated=True,
        )

    def _terminal(self, att: _Attempt, state: Literal["done", "err", "x", "f"]) -> None:
        att.state = state
        att.ended_at = self._clock.now()
        att.scope.cancel()
        att.terminal.set()
        health = self._engine.health
        if health is not None:
            outcome: Literal["ok", "error", "throttled", "timeout"] = "ok"
            if att.error is not None:
                code = att.error.provider_code or ""
                outcome = (
                    "throttled"
                    if att.error.kind is ErrorKind.THROTTLED
                    else ("timeout" if code.startswith("timeout:") else "error")
                )
            ttft = None if att.first_text_at is None else round((att.first_text_at - att.started_mono) * 1000)
            health.record(att.model_id, self._ctx.tenant.home_region, self._ctx.tenant.aws_account_id, outcome, ttft)
        self._signal()

    def _complete(self, att: _Attempt) -> None:
        att.finish_reason = _FINISH[att.stop or "end_turn"]
        att.outcome = "committed"
        self._emit(
            {
                "type": "generation.completed",
                "attempt_id": att.attempt_id,
                "finish_reason": att.finish_reason,
                "usage": usage_wire(self._usage(att), float(self._cost(att).usd), self._cost(att).estimated),
            }
        )
        self._terminal(att, "done")

    def _cost(self, att: _Attempt) -> Cost:
        usage = self._usage(att)
        if self._engine.cost is None:
            return Cost(usd=Decimal(0), estimated=True)
        return self._engine.cost.price(att.alias, self._ctx.tenant.home_region, usage)

    def _cancel_attempt(self, att: _Attempt, reason: CancelReason) -> None:
        if att.is_terminal:
            return
        if att.committed:
            if reason not in ("client_abort", "deadline"):
                return
            if reason == "client_abort":
                self._emit({"type": "generation.cancelled", "attempt_id": att.attempt_id, "reason": "client_abort"})
            att.outcome, att.finish_reason = "committed", "cancelled"
        else:
            if reason != "deadline":
                self._emit({"type": "generation.cancelled", "attempt_id": att.attempt_id, "reason": reason})
            att.outcome, att.cancel_reason = "cancelled", reason
        self._terminal(att, "x")

    # --- attempt streams ---------------------------------------------------------------------------------------
    async def _run_attempt(self, att: _Attempt, req: ModelRequest) -> None:
        timeouts = self._ctx.snapshot.route_timeouts(att.alias, att.effort)
        with att.scope:
            try:
                adapter = self._engine.adapters.for_model(att.alias)
                async with adapter.stream(
                    req, tenant=self._ctx.tenant, timeouts=timeouts, deadline=self._ctx.deadline
                ) as stream:
                    async for ev in stream:
                        self._on_event(att, ev)
                        if att.is_terminal:
                            break
                if not att.is_terminal:
                    self._on_stream_end(att)
            except AdapterError as exc:
                self._on_error(att, exc.error)

    def _on_event(self, att: _Attempt, ev: object) -> None:
        if isinstance(ev, MessageStart | ReasoningDelta | TextDelta | Stop):
            self._upstream = True
        if isinstance(ev, MessageStart | ReasoningDelta):
            if att.state == "P":
                att.state = "S"
            if isinstance(ev, ReasoningDelta):
                att.reasoning_chars += ev.chars
        elif isinstance(ev, TextDelta):
            self._on_text(att, ev.text)
        elif isinstance(ev, Stop):
            att.stop = ev.reason
        elif isinstance(ev, UsageEvent):
            att.usage = ev.usage
        elif isinstance(ev, ErrorEvent):
            self._on_error(att, ev.error)

    def _on_text(self, att: _Attempt, text: str) -> None:
        att.text_chars += len(text)
        if att.committed:
            self._delta(att, text)
            return
        att.buffer.append(text)
        att.state = "B"
        self._note_first_text(att)
        if self._can_commit_now():
            self._do_commit(att, self._armed or self._auto_commit)

    def _on_stream_end(self, att: _Attempt) -> None:
        att.stream_ended = True
        if att.committed:
            self._complete(att)
            return
        if att.stop is None:
            self._on_error(att, ClassifiedError(ErrorKind.RETRYABLE, "stream ended without a stop", "no_stop"))
            return
        if att.text_chars == 0 and att.stop != "content_filter":
            code = "reasoning_only" if att.stop == "max_tokens" else "empty_text"
            self._on_error(att, ClassifiedError(ErrorKind.RETRYABLE, "no answer text", code))
            return
        if att.text_chars == 0:  # an empty committable answer (content filter, 04:L114)
            att.state = "B"
            self._note_first_text(att)
        if self._can_commit_now():
            self._do_commit(att, self._armed or self._auto_commit)

    def _on_error(self, att: _Attempt, err: ClassifiedError) -> None:
        if att.is_terminal:
            return
        if err.provider_code == "timeout:deadline" or self._ctx.deadline.expired():
            self._expire()  # T19 for the whole run: no frame, no walk; a committed attempt ends finish_reason cancelled
            return
        att.error = err
        if att.committed:
            code = ErrorCode.UPSTREAM_STALLED if err.provider_code == "timeout:idle" else walk_error(err.kind)
            self._emit(NoemaError(code, "The answer stream failed.").sse_event())
            att.outcome, att.error_code = "committed", code.value
            self._terminal(att, "err")
        else:
            self._emit({"type": "generation.cancelled", "attempt_id": att.attempt_id, "reason": "failed"})
            att.outcome, att.cancel_reason, att.error_code = "failed", "failed", walk_error(err.kind).value
            self._terminal(att, "f")

    def _expire(self) -> None:
        if self._cancelled is None:
            self._cancelled = "deadline"
        for other in self._attempts:
            self._cancel_attempt(other, "deadline")
        self._signal()

    def _record(self, att: _Attempt) -> AttemptRecord:
        assert att.outcome is not None and att.ended_at is not None
        return AttemptRecord(
            attempt_id=att.attempt_id,
            route_id=att.route_id,
            model=att.model_id,
            outcome=att.outcome,
            cancel_reason=att.cancel_reason,
            usage=self._usage(att),
            cost=self._cost(att),
            finish_reason=att.finish_reason,
            error_code=att.error_code,
            started_at=att.started_at,
            ended_at=att.ended_at,
            spec_signature=self._spec.signature(),
            first_text_at=None
            if att.first_text_at is None
            else att.started_at + timedelta(seconds=att.first_text_at - att.started_mono),
        )

    # --- the walk ---------------------------------------------------------------------------------------------
    def _admit(self, member: GenMemberCfg) -> bool:
        health = self._engine.health
        if health is None:
            return True
        model_id = self._ctx.snapshot.model(member.model).id
        return health.admit(model_id, self._ctx.tenant.home_region, self._ctx.tenant.aws_account_id)

    async def _start(self, route_id: str, member: GenMemberCfg, *, race: bool = False) -> _Attempt | None:
        snap = self._ctx.snapshot
        model = snap.model(member.model)
        prompt: AssembledPrompt = await self._prompt.build(member.model)
        if self._cancelled is not None:
            return None
        effort = self._spec.effort if self._spec.effort is not None else member.effort
        if effort is not None and effort not in model.controls.effort:
            effort = None
        cap = model.max_output_tokens_cap
        base = self._spec.max_output_tokens or member.max_output_tokens or cap or GEN_DEFAULT_MAX_TOKENS
        max_tokens = base + model.reasoning_reserve_tokens
        if cap is not None:
            max_tokens = min(cap, max_tokens)
        req = ModelRequest(
            model_alias=member.model,
            system=prompt.system,
            messages=prompt.messages,
            max_tokens=max_tokens,
            call_kind="generation",
            retry="default",
            effort=effort,
        )
        att = _Attempt(
            attempt_id=self._engine.ids.new("att"),
            route_id=route_id,
            alias=member.model,
            model_id=model.id,
            member=member,
            started_at=self._clock.now(),
            started_mono=self._now(),
            effort=effort,
            prompt_chars=sum(len(s) for s in prompt.system)
            + sum(len(b.text) for m in prompt.messages for b in m.content if isinstance(b, TextBlock)),
            race=race,
        )
        self._attempts.append(att)
        self._ctx.reservation.consume("generation", member.model)
        degradations = [
            Degradation(d.from_, d.to, d.reason, d.file_id, d.part_index, att.attempt_id, model.id).to_wire()
            for d in prompt.degradations
        ]
        self._emit(
            {
                "type": "generation.started",
                "attempt_id": att.attempt_id,
                "route_id": route_id,
                "model": model.id,
                "speculative": self._speculative,
                "degradations": list(degradations),
            }
        )
        assert self.tg is not None
        self.tg.start_soon(self._run_attempt, att, req)
        return att

    def _end_with_error(self, last: ClassifiedError | None) -> None:
        code = walk_error(last.kind if last is not None else ErrorKind.RETRYABLE)
        self._emit(NoemaError(code, "No route could answer.").sse_event())

    async def _await_race(self, atts: list[_Attempt]) -> None:
        while self._committed is None and not all(a.is_terminal for a in atts) and self._cancelled is None:
            changed = self._changed
            await changed.wait()

    async def drive(self) -> None:
        """The pre-commit walk (spec-04 §6); ends the run once every attempt is terminal."""
        try:
            await self._walk()
        finally:
            with anyio.CancelScope(shield=True):  # cancelled from outside: the run's exit marks the attempts terminal
                for att in self._attempts:
                    await att.terminal.wait()
            if not self._first_text.is_set():
                self._first_text.set()
            self._ended.set()
            self._signal()

    async def _walk(self) -> None:
        snap = self._ctx.snapshot
        profile = snap.generation_profile(self._spec.generation_profile)
        route = profile.id
        last: ClassifiedError | None = None
        queue: list[GenMemberCfg] = list(profile.members)
        first = True
        if profile.mode == "race":
            atts = [a for m in profile.members if self._admit(m) if (a := await self._start(route, m, race=True))]
            await self._await_race(atts)
            if self._committed is not None or self._cancelled is not None or not atts:
                if not atts:
                    self._end_with_error(None)
                return
            last = next((a.error for a in reversed(atts) if a.error is not None), None)
            queue, first = [], False
        elif profile.mode == "single":
            queue = queue[:1]
        used_default = route == snap.routing.default_profile
        throttle_retried = False
        while self._cancelled is None:
            member = next((m for m in queue if self._admit(m)), None)
            if member is not None:
                queue = queue[queue.index(member) + 1 :]
            elif not used_default:
                default = snap.generation_profile(snap.routing.default_profile)
                route, queue, used_default = default.id, list(default.members[:1]), True
                continue
            else:
                self._end_with_error(last)
                return
            if not first and not self._may_retry(last, member):
                self._end_with_error(last)
                return
            first = False
            att = await self._start(route, member)
            if att is None:
                return
            await att.terminal.wait()
            if att.outcome != "failed":
                return
            last = att.error
            if last is not None and last.kind is ErrorKind.TENANT_ACCESS:
                self._end_with_error(last)
                return
            if last is not None and last.kind is ErrorKind.THROTTLED and not queue and not throttle_retried:
                throttle_retried = True
                wait_s = last.retry_after_s if last.retry_after_s is not None else THROTTLE_RETRY_S
                need = snap.route_timeouts(member.model, att.effort).first_answer_s
                if self._ctx.deadline.at - self._now() >= wait_s + need:
                    await self._clock.sleep(wait_s)
                    queue = [member]

    def _may_retry(self, last: ClassifiedError | None, member: GenMemberCfg) -> bool:
        if last is not None and last.kind is ErrorKind.TENANT_ACCESS:
            return False
        if not self._budget.try_take():
            return False
        rb = self._engine.retry_budget
        model_id = self._ctx.snapshot.model(member.model).id
        return rb is None or rb.try_spend(self._ctx.tenant.aws_account_id, model_id)


class FakeGenerationEngine:
    """Implements `GenerationEngine` (see the module docstring). `runs` lists every run started."""

    def __init__(
        self,
        clock: Clock,
        ids: IdGen,
        adapters: AdapterRegistry,
        *,
        health: HealthRegistry | None = None,
        retry_budget: RetryBudget | None = None,
        cost: CostMeter | None = None,
    ) -> None:
        self.clock = clock
        self.ids = ids
        self.adapters = adapters
        self.health = health
        self.retry_budget = retry_budget
        self.cost = cost
        self.runs: list[FakeProfileRun] = []

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
    ) -> AbstractAsyncContextManager[FakeProfileRun]:
        return self._run(ctx, spec, prompt, hold, speculative, auto_commit, failure_budget)

    @asynccontextmanager
    async def _run(
        self,
        ctx: TurnContext,
        spec: AttemptSpec,
        prompt: PromptBuilder,
        hold: bool,
        speculative: bool,
        auto_commit: Literal["spec_agrees", "gate"],
        failure_budget: AttemptBudget,
    ) -> AsyncIterator[FakeProfileRun]:
        run = FakeProfileRun(
            self,
            ctx,
            spec,
            prompt,
            hold=hold,
            speculative=speculative,
            auto_commit=auto_commit,
            failure_budget=failure_budget,
        )
        self.runs.append(run)
        with single_errors():
            async with anyio.create_task_group() as tg:
                run.tg = tg
                tg.start_soon(run.drive)
                try:
                    yield run
                finally:
                    with anyio.CancelScope(shield=True):
                        await run.cancel("deadline")  # no-op when the run already ended
                        await run.result()
                    tg.cancel_scope.cancel()
