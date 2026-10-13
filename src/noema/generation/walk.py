"""`StreamingProfileRun`: the profile modes and the pre-commit failure walk (spec-04 §6, §4.1 T1/T2/T11/T12;
IMPL-015, IMPL-016, IMPL-084).

- `single`: one attempt; `fallback`: the members in order, each a new attempt with the same spec; `race`: every
  member starts at once (one generation.started each, in member order), the earliest first text wins at the commit
  point and the others end lost_race; race members never spend the failure budget.
- On a pre-commit failure (T10): the next member (fallback mode; a member whose breaker is open, that the tenant may
  not use or whose prompt does not fit its context window is skipped without an attempt), then the first member of
  `routing.default_profile` if that profile differs, else the run ends with `error{code of the last failure}`
  (T12). Guards for every failure-driven attempt: the turn's `AttemptBudget` (the profile attempt cap) and "not
  tenant_access" (fail closed); `RetryBudget.try_spend` only for a same-model re-send (a model an earlier attempt of
  this run already called: the default profile's member after the route's, the throttle retry). A fallback to a
  different model is not a retry and never spends the retry budget of either model (owner ruling R6, IMPL-016):
  admission's worst-case reservation and the per-(account, model) buckets bound it. A throttle falls back at once
  when a next member exists; otherwise one same-member retry after `retry_after_s` or full jitter, if ctx.deadline
  still leaves the route's first-answer timeout (the retry's guards are checked before the backoff, so a refused
  retry does not delay the error).
- Connect/DNS/TLS errors before any response byte are retried once inside the adapter (`retry="default"`, the
  same attempt, spec-04 §6 step 1); the engine sees only the classified result.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Literal

import anyio
import anyio.abc

from noema.config.models import GenMemberCfg
from noema.core.errors import ClassifiedError, ErrorKind, NoemaError
from noema.core.protocols.adapters import ModelRequest
from noema.core.values import Degradation, JsonValue

from .attempt import Attempt
from .policy import attempt_max_tokens, effective_effort, throttle_backoff_s, walk_error
from .run import NO_ROUTE, RunCore, log
from .spans import start_attributes

StartResult = Attempt | Literal["skipped", "refused", "stopped"]


class StreamingProfileRun(RunCore):
    """Implements `ProfileRun` (spec-02 §10); built and driven by `StreamingGenerationEngine.run`."""

    def start(self, tg: anyio.abc.TaskGroup) -> None:
        """Start the walk and arm the run's deadline (T19) in the engine's task group."""
        self._tg = tg
        tg.start_soon(self._drive)
        self._arm_deadline(tg)

    async def _drive(self) -> None:
        try:
            with self._walk_scope:
                await self._walk()
        finally:
            with anyio.CancelScope(shield=True):
                for att in list(self._attempts):
                    await att.terminal.wait()
            self._finish()

    async def _walk(self) -> None:
        if self._stopped:
            return
        snap = self._ctx.snapshot
        profile = self._profile
        route = profile.id
        queue: list[GenMemberCfg]
        if profile.mode == "race":
            started = await self._race(route, profile.members)
            if self._committed is not None or self._stopped:
                return
            queue, failure = [], started > 0  # every race member failed (or none could start): step 3
        else:
            queue = list(profile.members[:1] if profile.mode == "single" else profile.members)
            failure = False
        default_id = snap.routing.default_profile
        used_default = route == default_id
        throttle_retried = prepaid = False
        while not self._stopped:
            if self._tenant_access:  # fail closed: a tenant-access failure never falls back (ADR-001)
                self._end_with_error()
                return
            if not queue:
                if used_default:
                    self._end_with_error()
                    return
                default = snap.generation_profile(default_id)
                route, queue, used_default = default.id, list(default.members[:1]), True
                continue
            member = queue.pop(0)
            result = await self._start(route, member, failure=failure, prepaid=prepaid)
            prepaid = False
            if result == "skipped":
                continue
            if result == "refused":
                self._end_with_error()
                return
            if result == "stopped":
                return
            failure = True
            await result.terminal.wait()
            if result.outcome != "failed":
                return
            err = result.error
            if err is not None and err.kind is ErrorKind.THROTTLED and not queue and not throttle_retried:
                throttle_retried = True
                if await self._throttle_wait(member, result, err):
                    queue, prepaid = [member], True

    async def _race(self, route: str, members: tuple[GenMemberCfg, ...]) -> int:
        """Start every admitted member, then wait for a commit, a stop, or every member terminal."""
        started: list[Attempt] = []
        for member in members:
            if self._committed is not None or self._stopped:
                break
            result = await self._start(route, member, failure=False)
            if isinstance(result, Attempt):
                started.append(result)
        while self._committed is None and not self._stopped and not all(a.is_terminal for a in started):
            changed = self._changed
            await changed.wait()
        return len(started)

    async def _throttle_wait(self, member: GenMemberCfg, att: Attempt, err: ClassifiedError) -> bool:
        """The one same-member retry after a throttle with no next member (IMPL-016). The guards run before the
        backoff, so a refused retry ends the walk at once: the turn's `AttemptBudget` has a unit left, ctx.deadline
        still leaves the route's first-answer timeout after the wait, and `RetryBudget.try_spend` grants the retry
        (spent here, so the retry's `_start` does not spend it again). Then sleeps; True iff the run still runs."""
        if self._budget.remaining <= 0:
            return False
        wait = throttle_backoff_s(err.retry_after_s, self._failure_attempts, self._deps.rng)
        need = self._ctx.snapshot.route_timeouts(member.model, att.effort).first_answer_s
        if self._ctx.deadline.at - self._now() < wait + need:
            return False
        if not self._deps.retry_budget.try_spend(self._ctx.tenant.aws_account_id, att.model_id):
            return False
        await self._clock.sleep(wait)
        return not self._stopped

    def _end_with_error(self) -> None:
        """T12: error{code of the last failure's ErrorKind} (IMPL-076); no attempt at all -> all_routes_unavailable."""
        last = self._last_error
        kind = ErrorKind.TENANT_ACCESS if self._tenant_access else (last.kind if last is not None else None)
        self._emit(NoemaError(walk_error(kind), NO_ROUTE).sse_event())

    async def _start(self, route_id: str, member: GenMemberCfg, *, failure: bool, prepaid: bool = False) -> StartResult:
        """T1/T2: start one attempt on `member`, or skip it (breaker open, model not allowed for the tenant, prompt
        too large for its context window), or refuse it (a failure-driven attempt whose guards fail). `prepaid`:
        the `RetryBudget` unit was already spent (the throttle retry)."""
        ctx = self._ctx
        snap, tenant = ctx.snapshot, ctx.tenant
        model = snap.model(member.model)
        allowed = snap.tenant(tenant.tenant_id).allowed_models
        if allowed is not None and member.model not in allowed:
            return "skipped"
        if not self._deps.health.admit(model.id, tenant.home_region, tenant.aws_account_id):
            return "skipped"
        if failure and self._budget.remaining <= 0:
            return "refused"
        effort, effort_degradation = effective_effort(self._spec.effort, member, model)
        max_tokens = attempt_max_tokens(self._spec.max_output_tokens, member, model)
        prompt = await self._prompt.build(member.model)  # media renegotiated for this model (spec-07 §4)
        if self._ctx.deadline.expired():
            self._expire()  # T19 even if the deadline watcher has not run yet
        if self._stopped or self._committed is not None:
            return "stopped"
        blocks = [block for message in prompt.messages for block in message.content]
        input_tokens = self._deps.estimator.estimate(member.model, prompt.system, blocks)
        if input_tokens + max_tokens > model.context_window:  # capability fit (IMPL-084)
            log.info("generation member skipped: context window", extra={"model": model.id})
            return "skipped"
        if failure:
            resend = any(a.model_id == model.id for a in self._attempts)  # R6: only a same-model re-send is a retry
            spent = prepaid or not resend or self._deps.retry_budget.try_spend(tenant.aws_account_id, model.id)
            if not spent or not self._budget.try_take():
                return "refused"
            self._failure_attempts += 1
        att = Attempt(
            attempt_id=self._deps.ids.new("att"),
            route_id=route_id,
            alias=member.model,
            model_id=model.id,
            effort=effort,
            max_tokens=max_tokens,
            input_tokens=input_tokens,
            started_at=self._clock.now(),
            started_mono=self._now(),
            index=len(self._attempts),
            client_capped=max_tokens < attempt_max_tokens(None, member, model),
        )
        self._attempts.append(att)
        ctx.reservation.consume("generation", member.model)
        found = (*prompt.degradations, effort_degradation) if effort_degradation else prompt.degradations
        degradations = [replace(d, attempt_id=att.attempt_id, model=model.id) for d in found]
        self._started_event(att, degradations)
        request = ModelRequest(
            model_alias=member.model,
            system=prompt.system,
            messages=prompt.messages,
            max_tokens=max_tokens,
            call_kind="generation",
            retry="default",
            effort=effort,
            snapshot=snap,
        )
        attrs = start_attributes(
            model_id=model.id,
            tenant_id=tenant.tenant_id,
            route_id=route_id,
            spec_signature=self._signature,
            session_id=ctx.session_id,
            prompt=prompt,
            degradations=degradations,
        )
        assert self._tg is not None
        self._tg.start_soon(self._run_attempt, att, request, snap.route_timeouts(member.model, effort), attrs)
        return att

    def _started_event(self, att: Attempt, degradations: list[Degradation]) -> None:
        instruments = self._deps.telemetry.instruments
        for d in degradations:
            instruments.add("aghoo_degradations_total", reason=d.reason, model=att.model_id)
        wire: list[JsonValue] = [d.to_wire() for d in degradations]
        self._emit(
            {
                "type": "generation.started",
                "attempt_id": att.attempt_id,
                "route_id": att.route_id,
                "model": att.model_id,
                "speculative": self._speculative,
                "degradations": wire,
            }
        )
