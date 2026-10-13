"""`MemberCallRunner`: one member call unit (a flat question, or one hierarchical round/group) with time-triggered
backups, health checks, reservation, parsing and usage (spec-06 §5.6-§5.7, §6; IMPL-020, IMPL-050, IMPL-076).

Decision member calls are never retried. A backup is an identical call sent at `backup_after_ms` while the
primary is in flight, iff `backup_after_ms + latency_p95_ms` fits the round's budget. The first call to finish
`ok` or `refused` wins and the other is cancelled; an `error` waits for the other call.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal

import anyio
import anyio.abc
import anyio.lowlevel

from noema.core.clock import Clock, Deadline, deadline_scope
from noema.core.errors import ErrorKind
from noema.core.protocols.adapters import AdapterError, AdapterRegistry, ModelRequest, ModelResponse
from noema.core.protocols.decision import DecisionContext, OptionSpec
from noema.core.protocols.ops import CostMeter, HealthRegistry, TokenEstimator
from noema.core.protocols.telemetry import Telemetry
from noema.core.values import CallKind, UsageRecord

from .elicitation.logprob_codes import parse_logprob
from .elicitation.parsed import PARSE_MESSAGES, Parsed, parse_error
from .elicitation.verbalized import parse_verbalized
from .state import CallRec, Slot

_log: Final = logging.getLogger("noema.decision.members")
UnitStatus = Literal["ok", "refused", "error", "timeout"]
ERROR_MESSAGES: Final[Mapping[str, str]] = {
    "throttled": "Bedrock throttled the call (ThrottlingException); member calls are not retried",
    "upstream_error": "The model call failed upstream.",
    "invalid_request": "Bedrock rejected the member request.",
    "tenant_access_misconfigured": "The tenant's Bedrock access is misconfigured for this model.",
    "circuit_open": "The model's circuit breaker is open.",
    "model_not_allowed": "The model is not enabled for this tenant.",
    "censored_tail": "A hierarchical round read a censored logprob tail.",
    **PARSE_MESSAGES,
}
_KIND_CODE: Final[Mapping[ErrorKind, str]] = {
    ErrorKind.THROTTLED: "throttled",
    ErrorKind.RETRYABLE: "upstream_error",
    ErrorKind.FATAL: "upstream_error",
    ErrorKind.INVALID_REQUEST: "invalid_request",
    ErrorKind.TENANT_ACCESS: "tenant_access_misconfigured",
}


@dataclass(slots=True)
class CallUnit:
    """What one member must answer: `options` in letter order (letter j = options[j]); `round` 0 for a flat
    question, 1 or 2 for a hierarchical one (`group` in round 2); `deadline` bounds every call; `budget_ms` decides
    whether a backup fits. The coordinator owns `scope` and reads `calls` for usage."""

    stage: int
    qname: str
    slot: Slot
    round: int
    group: str | None
    options: tuple[OptionSpec, ...]
    request: ModelRequest
    deadline: float
    budget_ms: int
    scope: anyio.CancelScope = field(default_factory=anyio.CancelScope)
    calls: list[CallRec] = field(default_factory=list)
    done: bool = False


@dataclass(frozen=True, slots=True)
class UnitOutcome:
    """The member's answer to one unit: `dist`/`censored` are keyed by option key."""

    status: UnitStatus
    ended_at: float
    dist: dict[str, float] | None = None
    unresolved: float = 0.0
    censored: tuple[str, ...] = ()
    error: tuple[str, str] | None = None
    region: str | None = None
    model_revision: str | None = None


def error_outcome(code: str, ended_at: float, status: UnitStatus = "error") -> UnitOutcome:
    """An outcome carrying a member error code and its short fixed message (never model output)."""
    return UnitOutcome(status, ended_at, error=(code, ERROR_MESSAGES.get(code, "The member call failed.")))


class MemberCallRunner:
    """Runs call units for one decision run (see the module docstring). Sync hot-path collaborators only
    (`HealthRegistry`, `Reservation`, `TokenEstimator`, `CostMeter`; IMPL-078)."""

    def __init__(
        self,
        *,
        clock: Clock,
        adapters: AdapterRegistry,
        health: HealthRegistry,
        estimator: TokenEstimator,
        cost: CostMeter,
        telemetry: Telemetry,
        ctx: DecisionContext,
    ) -> None:
        self._clock = clock
        self._adapters = adapters
        self._health = health
        self._estimator = estimator
        self._cost = cost
        self._telemetry = telemetry
        self._ctx = ctx
        self._kind: CallKind = "verification_member" if ctx.purpose == "verification" else "decision_member"
        tenant = ctx.snapshot.tenant(ctx.tenant.tenant_id)
        self._allowed = frozenset(tenant.allowed_models) if tenant.allowed_models is not None else None

    # --- usage of calls -------------------------------------------------------------------------------------------
    def mark_cancelled(self, rec: CallRec, unit: CallUnit, now: float) -> None:
        """A cancelled call (lost race, close, deadline, run cancel): estimated input and `max_tokens` output, on
        the waste line (a sent non-streaming call is assumed billed, IMPL-020)."""
        if not rec.in_flight:
            return
        blocks = [b for m in unit.request.messages for b in m.content]
        estimate = self._estimator.estimate(rec.model_alias, unit.request.system, blocks)
        usage = UsageRecord(input_tokens=estimate, output_tokens=rec.max_tokens, tokens_estimated=True)
        region = self._ctx.tenant.home_region
        rec.outcome, rec.ended_at, rec.usage, rec.region = "cancelled", now, usage, region
        rec.cost = self._cost.price(rec.model_alias, region, usage, snapshot=self._ctx.snapshot)

    def _completed(self, rec: CallRec, resp: ModelResponse, now: float) -> None:
        if not rec.in_flight:  # the coordinator cancelled it in the same instant: the estimate stands
            return
        rec.outcome, rec.ended_at, rec.usage, rec.region = "completed", now, resp.usage, resp.region
        # priced in the tenant's home region (not the serving one) with the pinned catalog (spec-11 §2, IMPL-019)
        home = self._ctx.tenant.home_region
        rec.cost = self._cost.price(rec.model_alias, home, resp.usage, snapshot=self._ctx.snapshot)

    # --- one unit ------------------------------------------------------------------------------------------------
    async def run(self, unit: CallUnit) -> UnitOutcome:
        """The member's outcome for `unit` (never raises for provider failures)."""
        member = unit.slot.member
        now = self._clock.monotonic()
        if self._allowed is not None and member.cfg.model not in self._allowed:
            return error_outcome("model_not_allowed", now)
        region, account = self._ctx.tenant.home_region, self._ctx.tenant.aws_account_id
        if not self._health.admit(member.model.id, region, account):
            return error_outcome("circuit_open", now)
        cfg = member.cfg
        backup_after: float | None = None
        if (
            cfg.backup_after_ms is not None
            and cfg.latency_p95_ms is not None
            and cfg.backup_after_ms + cfg.latency_p95_ms <= unit.budget_ms
        ):
            backup_after = cfg.backup_after_ms / 1000
        if backup_after is None:  # no backup can fire: the call runs in this task (no task group, stream or task)
            rec = self._record(unit, backup=False)
            await anyio.lowlevel.checkpoint()  # the hop a hedged call's task takes: the units keep their dispatch order
            return await self._outcome(unit, rec)
        send, recv = anyio.create_memory_object_stream[tuple[CallRec, UnitOutcome]](2)
        recs: list[CallRec] = []
        with send, recv:
            return await self._hedge(unit, send, recv, recs, backup_after)

    async def _hedge(
        self,
        unit: CallUnit,
        send: anyio.abc.ObjectSendStream[tuple[CallRec, UnitOutcome]],
        recv: anyio.abc.ObjectReceiveStream[tuple[CallRec, UnitOutcome]],
        recs: list[CallRec],
        backup_after: float | None,
    ) -> UnitOutcome:
        member = unit.slot.member
        region, account = self._ctx.tenant.home_region, self._ctx.tenant.aws_account_id
        last: UnitOutcome | None = None
        async with anyio.create_task_group() as tg:
            recs.append(self._launch(tg, unit, send, backup=False))
            backup_at = recs[0].started_at + backup_after if backup_after is not None else None
            pending = 1
            while True:
                if backup_at is not None:
                    async with deadline_scope(self._clock, backup_at) as waiting:
                        _, outcome = await recv.receive()
                    if waiting.cancelled_caught:
                        backup_at = None
                        if self._health.admit(member.model.id, region, account):
                            recs.append(self._launch(tg, unit, send, backup=True))
                            pending += 1
                        continue
                else:
                    _, outcome = await recv.receive()
                pending -= 1
                if outcome.status in ("ok", "refused") or pending == 0 or backup_at is not None:
                    last = outcome  # a primary failing before backup_after_ms is final: no backup, no retry
                    break
                last = outcome
            now = self._clock.monotonic()
            for other in recs:
                self.mark_cancelled(other, unit, now)
            tg.cancel_scope.cancel()
        assert last is not None
        return last

    def _launch(
        self,
        tg: anyio.abc.TaskGroup,
        unit: CallUnit,
        send: anyio.abc.ObjectSendStream[tuple[CallRec, UnitOutcome]],
        *,
        backup: bool,
    ) -> CallRec:
        rec = self._record(unit, backup=backup)
        tg.start_soon(self._attempt, unit, rec, send)
        return rec

    def _record(self, unit: CallUnit, *, backup: bool) -> CallRec:
        """The call's record (on the unit and its slot) and its reservation debit, before it is sent."""
        member = unit.slot.member
        rec = CallRec(
            started_at=self._clock.monotonic(),
            model_alias=member.cfg.model,
            model_id=member.model.id,
            max_tokens=unit.request.max_tokens,
            input_estimate=0,
            backup=backup,
            round=unit.round,
            group=unit.group,
        )
        unit.calls.append(rec)
        unit.slot.calls.append(rec)
        self._ctx.reservation.consume(self._kind, member.cfg.model)
        return rec

    async def _attempt(
        self, unit: CallUnit, rec: CallRec, send: anyio.abc.ObjectSendStream[tuple[CallRec, UnitOutcome]]
    ) -> None:
        await send.send((rec, await self._outcome(unit, rec)))

    async def _outcome(self, unit: CallUnit, rec: CallRec) -> UnitOutcome:
        """One call under its `decision.member` span."""
        member = unit.slot.member
        attrs: dict[str, str | int | float | bool] = {
            "aghoo.member.id": member.member_id,
            "aghoo.member.question": unit.qname,
            "aghoo.member.elicitation": member.cfg.elicitation,
            "aghoo.member.input_view": member.view_kind,
            "gen_ai.request.model": member.model.id,
            "aghoo.member.backup": rec.backup,
        }
        if unit.round:
            attrs["aghoo.decision.round"] = unit.round
            if unit.group is not None:
                attrs["aghoo.decision.group"] = unit.group
        with self._telemetry.span("decision.member", **attrs) as span:
            outcome = await self._call(unit, rec)
            span.set("aghoo.member.status", outcome.status)
            if outcome.error is not None:
                span.error(outcome.error[0])
        return outcome

    async def _call(self, unit: CallUnit, rec: CallRec) -> UnitOutcome:
        alias = unit.slot.member.cfg.model
        try:
            adapter = self._adapters.for_model(alias)
            resp = await adapter.complete(
                unit.request, tenant=self._ctx.tenant, deadline=Deadline(unit.deadline, self._clock)
            )
        except AdapterError as exc:
            return self._failed(unit, rec, exc.error.kind, exc.error.provider_code)
        except Exception as exc:  # an adapter bug: classified, never raised out of the run
            _log.error("adapter call failed unclassified: %s", type(exc).__name__)
            return self._failed(unit, rec, ErrorKind.FATAL, None)
        now = self._clock.monotonic()
        self._completed(rec, resp, now)
        # a unary call has no TTFT: its whole latency grows with the tenant's input and reasoning, so it is no sample
        # for the shared per-(model, region) breaker's p95-TTFT rule (one tenant could open it for all; Wave E)
        self._health.record(unit.slot.member.model.id, resp.region, self._ctx.tenant.aws_account_id, "ok", None)
        return self._parse(unit, resp, now)

    def _failed(self, unit: CallUnit, rec: CallRec, kind: ErrorKind, provider_code: str | None) -> UnitOutcome:
        now = self._clock.monotonic()
        model_id, account = unit.slot.member.model.id, self._ctx.tenant.aws_account_id
        region = self._ctx.tenant.home_region
        if provider_code == "timeout:deadline" or now >= unit.deadline:
            # the caller's budget ran out, not the provider: no breaker signal (as in generation), or a model a
            # little slower than the router deadline, or one pod's loop lag, would open the circuit on every pod
            self.mark_cancelled(rec, unit, now)
            return UnitOutcome("timeout", now)
        if rec.in_flight:
            rec.outcome, rec.ended_at, rec.region = "failed", now, region
        # only provider health goes into the shared per-(model, region) breaker (BreakerRegistry contract): one
        # tenant's broken grant (tenant_access) or bad request (invalid_request) is not an outage of the model
        if kind is ErrorKind.THROTTLED:
            self._health.record(model_id, region, account, "throttled", None)
        elif kind in (ErrorKind.RETRYABLE, ErrorKind.FATAL):
            timeout = (provider_code or "").startswith("timeout:")
            self._health.record(model_id, region, account, "timeout" if timeout else "error", None)
        return error_outcome(_KIND_CODE[kind], now)

    def _parse(self, unit: CallUnit, resp: ModelResponse, now: float) -> UnitOutcome:
        if resp.refused:
            return UnitOutcome("refused", now, region=resp.region, model_revision=resp.model_revision)
        letters = [chr(65 + j) for j in range(len(unit.options))]
        member = unit.slot.member
        try:
            parsed: Parsed = (
                parse_logprob(resp.logprobs, letters)
                if member.template.logprobs
                else parse_verbalized(resp.text, letters, resp.stop_reason)
            )
        except Exception:  # a parser bug on this output: the member's parse error, not an adapter error
            _log.error("member output parse failed unclassified", exc_info=True)
            parsed = parse_error("parse_error")
        if parsed.error is not None or parsed.dist is None:
            code = parsed.error or "parse_error"
            self._telemetry.instruments.add("aghoo_decision_parse_errors_total", member=member.cfg.model, code=code)
            return error_outcome(code, now)
        if unit.round and parsed.censored:  # a censored group or in-group read has no representation (R)
            return error_outcome("censored_tail", now)
        return UnitOutcome(
            "ok",
            now,
            dist={unit.options[ord(letter) - 65].key: p for letter, p in parsed.dist.items()},
            unresolved=parsed.unresolved,
            censored=tuple(unit.options[ord(letter) - 65].key for letter in parsed.censored),
            region=resp.region,
            model_revision=resp.model_revision,
        )


def letters_for(options: Sequence[OptionSpec]) -> list[str]:
    """`A`, `B`, ... for the options in letter order."""
    return [chr(65 + j) for j in range(len(options))]
