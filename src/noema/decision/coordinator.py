"""The run coordinator: one task owns every answer's state (spec-06 §6-§10, §19.4; IMPL-001, IMPL-002, IMPL-025).

Member call units and timers report through an unbounded memory stream; the coordinator closes answers (every
required member terminal, quorum impossible, the stage or round-1 deadline, or cancel), runs the hierarchical
rounds, plans escalation stages, and publishes S1 and the final snapshot. Tasks are spawned from the coordinator
task only, so member spans are children of the `decision` (or `decision.escalation`) span.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from contextlib import ExitStack, suppress
from dataclasses import dataclass, field, replace
from typing import Final

import anyio
import anyio.abc
import anyio.lowlevel

from noema.config.models import DecisionProfileCfg
from noema.core.protocols.decision import CompiledQuestion, DecisionContext, DecisionRequest, OptionSpec
from noema.core.values import JsonValue, Modality

from .answer import classify, qcount, quorum_wire, unscored_answer, vote_base
from .compiler import flat_limit, option_count
from .deps import EngineDeps
from .elicitation.request import member_request
from .escalation import filtered_in, fired_triggers, union
from .flat import AnswerContext, flat_scored, flat_votes
from .hier_answer import hier_scored, hier_votes
from .hierarchical import HIER_ROUND1_SHARE, expand, group_options, need
from .members import CallUnit, MemberCallRunner, UnitOutcome, error_outcome
from .pool import linear_pool
from .signatures import input_view_signature, permutation
from .state import MemberStatus, Slot, StageInfo, StageMember
from .views import ViewsExpired, stage_members

_log: Final = logging.getLogger("noema.decision.coordinator")
# Loop passes a member call waits before its CPU-heavy first step (render, build, sign: ~1 ms each). A turn starts
# its generation attempt right after the decision entry, and that attempt needs about 4 task hops (profile run,
# attempt, stream open) to reach the network: without the yields it went out only after all 15 router-default
# calls had been dispatched (+15 ms TTFT; C2b perf smoke, reports/perf/c2b-perf-smoke.md). Ordering only: no time.
DISPATCH_YIELDS: Final = 6
MIN_DISPATCH_S: Final = 0.050  # MIN_DISPATCH_MS (spec-06 §2.3), also before hierarchical round 2


@dataclass(slots=True)
class QState:
    """One question across its stages. `answer` is the last closed stage answer; `pending` an eligible escalation
    `(target profile, fired triggers)`; `round` 1/2 while hierarchical rounds run (0 for flat)."""

    q: CompiledQuestion
    stage: int = 0
    slots: list[Slot] = field(default_factory=list)
    units: list[CallUnit] = field(default_factory=list)
    closed: bool = False
    final: bool = False
    answer: dict[str, JsonValue] | None = None
    pending: tuple[DecisionProfileCfg, tuple[str, ...]] | None = None
    round: int = 0
    d1: float = 0.0
    expanded_run: list[str] = field(default_factory=list)
    timers: list[anyio.CancelScope] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _UnitDone:
    unit: CallUnit
    outcome: UnitOutcome


@dataclass(frozen=True, slots=True)
class _Timer:
    stage: int
    qname: str | None  # None: the stage deadline; a name: that question's round-1 deadline


@dataclass(frozen=True, slots=True)
class _StageReady:
    stage: StageInfo


@dataclass(frozen=True, slots=True)
class _Cancel:
    pass


@dataclass(frozen=True, slots=True)
class _StageFailed:
    pass


@dataclass(frozen=True, slots=True)
class _StageExpired:  # the views were still resolving at the stage's latest start (§9.3 budget)
    pass


@dataclass(frozen=True, slots=True)
class _S1Due:
    pass


@dataclass(frozen=True, slots=True)
class _Recheck:
    qname: str
    stage: int


Event = _UnitDone | _Timer | _StageReady | _StageFailed | _StageExpired | _Cancel | _S1Due | _Recheck
# S1 and the "quorum impossible" closure wait one microsecond on the injected clock, so they also see the member
# results of the same instant (deterministic under a virtual clock, negligible in real time).
COALESCE_S: Final = 1e-6


class Coordinator:
    """Owns one run's state (see the module docstring). `on_s1` / `on_final` are called once each (sync) when the
    commit-critical answers, resp. every answer, become final."""

    def __init__(
        self,
        deps: EngineDeps,
        ctx: DecisionContext,
        req: DecisionRequest,
        *,
        seed: int,
        stage0: StageInfo,
        modalities: frozenset[Modality],
        on_s1: Callable[[], None],
        on_final: Callable[[], None],
        on_fail: Callable[[BaseException], None],
    ) -> None:
        self.deps, self.ctx, self.req, self.seed = deps, ctx, req, seed
        self.stages: list[StageInfo] = [stage0]
        self.qstates = [QState(q) for q in req.questions.questions]
        self.by_name = {qs.q.name: qs for qs in self.qstates}
        self.modalities = modalities
        self.cancelled = False
        self.budget_skipped = False
        self.final_published = False
        self.s1_published = False
        self.closed_stages: set[int] = set()
        self.all_slots: list[tuple[int, Slot]] = []  # every slot of every stage (usage per stage and in total)
        self.runner = MemberCallRunner(
            clock=deps.clock,
            adapters=deps.adapters,
            health=deps.health,
            estimator=deps.estimator,
            cost=deps.cost,
            telemetry=deps.telemetry,
            ctx=ctx,
        )
        self.actx = AnswerContext(
            ctx.tenant.tenant_id, ctx.tenant.home_region, ctx.snapshot.routing.confidence_bands, deps.calibrators
        )
        self._on_s1, self._on_final, self._on_fail = on_s1, on_final, on_fail
        self._send, self._recv = anyio.create_memory_object_stream[Event](math.inf)
        self._tg: anyio.abc.TaskGroup | None = None
        self._stage_timers: dict[int, anyio.CancelScope] = {}
        self._spans = ExitStack()
        self._quorum_seen = False
        self._s1_due = False
        self._rechecks: set[str] = set()
        self._kind = "verification_member" if ctx.purpose == "verification" else "decision_member"

    @property
    def clock_now(self) -> float:
        return self.deps.clock.monotonic()

    # --- task plumbing ---------------------------------------------------------------------------------------------
    def emit(self, event: Event) -> None:
        """Queue an event for the coordinator (sync; dropped once the run is over)."""
        with suppress(anyio.ClosedResourceError, anyio.BrokenResourceError):
            self._send.send_nowait(event)

    def cancel(self) -> None:
        """Request a cancel (handled in the coordinator task)."""
        self.emit(_Cancel())

    async def run(self, tg: anyio.abc.TaskGroup) -> None:
        """The coordinator task: start stage 0 and process events until every answer is final."""
        self._tg = tg
        profile = self.stages[0].profile
        attrs: dict[str, str | int | float | bool] = {
            "aghoo.profile": profile.id,
            "aghoo.profile_version": str(profile.version),
            "aghoo.decision.cache_hit": False,
        }
        with self.deps.telemetry.span("decision", **attrs) as span, self._recv, self._send:
            try:
                self._start(self.stages[0], self.qstates)
                while not self.final_published:
                    self._handle(await self._recv.receive())
            except Exception as exc:  # a coordinator bug: fail the run's waiters instead of hanging them
                _log.exception("decision coordinator failed")
                self.final_published = True
                self._abandon_units()
                self._on_fail(exc)
            finally:
                if not self.final_published:  # cancelled from outside: close every answer now (sync)
                    self._cancel()
                self._shutdown()
                self._spans.close()  # an escalation span still open (LIFO inside the decision span)
            span.set("aghoo.decision.calibration_status", self.calibration_status())

    def _abandon_units(self) -> None:
        """After a coordinator bug: stop every in-flight call, estimated on the waste line (it was sent)."""
        now = self.clock_now
        for qs in self.qstates:
            for unit in list(qs.units):
                try:
                    self._drop_unit(qs, unit, now)
                except Exception:
                    _log.exception("abandoning a member call unit failed")

    def _shutdown(self) -> None:
        for scope in self._stage_timers.values():
            scope.cancel()
        for qs in self.qstates:
            for unit in qs.units:
                unit.scope.cancel()
            for timer in qs.timers:
                timer.cancel()

    def _spawn_unit(self, unit: CallUnit) -> None:
        async def task() -> None:
            outcome: UnitOutcome | None = None
            with unit.scope:
                try:
                    for _ in range(DISPATCH_YIELDS):
                        await anyio.lowlevel.checkpoint()
                    outcome = await self.runner.run(unit)
                except Exception:  # a bug below the runner must not take the run down: the member errs
                    _log.exception("member call unit failed")
                    outcome = error_outcome("upstream_error", self.clock_now)
            if outcome is not None:
                self.emit(_UnitDone(unit, outcome))

        assert self._tg is not None
        self._tg.start_soon(task)

    def _spawn_timer(self, at: float, event: Event) -> anyio.CancelScope:
        scope = anyio.CancelScope()
        clock = self.deps.clock

        async def task() -> None:
            with scope:
                await clock.sleep(max(0.0, at - clock.monotonic()))
                self.emit(event)

        assert self._tg is not None
        self._tg.start_soon(task)
        return scope

    def calibration_status(self) -> str:
        """`calibrated`, `uncalibrated` or `mixed` over the scored final answers (the `decision` span)."""
        statuses = set()
        for qs in self.qstates:
            cal = (qs.answer or {}).get("calibration")
            if isinstance(cal, dict):
                statuses.add(cal.get("status"))
        if statuses == {"calibrated"}:
            return "calibrated"
        return "mixed" if "calibrated" in statuses else "uncalibrated"

    # --- stages and units ----------------------------------------------------------------------------------------
    def _start(self, stage: StageInfo, questions: list[QState]) -> None:
        stage.questions = tuple(qs.q.name for qs in questions)
        for qs in questions:
            qs.stage, qs.closed, qs.pending, qs.units, qs.round = stage.index, False, None, [], 0
            qs.slots = [Slot(m) for m in stage.members]
            self.all_slots.extend((stage.index, slot) for slot in qs.slots)
            for slot in qs.slots:
                if slot.member.skipped:
                    slot.status, slot.skip_reason = "skipped", "input_view"
        hopeless: set[str] = set()  # quorum impossible before any call: closed at once, no call (§19.3, §7.3 rule 4)
        for qs in questions:
            hier = qs.q.method == "hierarchical" and stage.index == 0
            if not (self._start_round1(qs, stage) if hier else self._start_flat(qs, stage)):
                hopeless.add(qs.q.name)
        self._stage_timers[stage.index] = self._spawn_timer(stage.deadline, _Timer(stage.index, None))
        for qs in questions:
            if qs.q.name in hopeless:
                self._close(qs, "late")
            else:
                self._check(qs)

    def _budget_ms(self, start: float, end: float) -> int:
        return max(0, math.floor((end - start) * 1000 + 1e-6))

    def _quorum_possible(self, qs: QState, stage: StageInfo) -> bool:
        """The members still to run can make `q_req` (else the answer closes with no call)."""
        return qcount(s for s in qs.slots if s.status is None) >= stage.min_quorum

    def _start_flat(self, qs: QState, stage: StageInfo) -> bool:
        """Launch every runnable member; False (nothing launched) when quorum is already impossible."""
        if not self._quorum_possible(qs, stage):
            return False
        budget = self._budget_ms(stage.t0, stage.deadline)
        declared = qs.q.options
        if qs.q.type in ("predicate", "noul"):  # the criteria render in their own object, not as descriptions
            declared = tuple(replace(o, description=None) for o in declared)
        for slot in qs.slots:
            if slot.status is None:
                options = self._order(stage, slot.member, qs.q.name, declared, qs.q.type == "choice")
                self._launch(qs, stage, slot, 0, None, options, stage.deadline, budget, qs.q.criteria)
        return True

    def _start_round1(self, qs: QState, stage: StageInfo) -> bool:
        """Skip the members that cannot carry `need`, then launch round 1; False as in `_start_flat`."""
        qs.round = 1
        qs.d1 = stage.t0 + math.floor((stage.deadline - stage.t0) * 1000 * HIER_ROUND1_SHARE + 1e-6) / 1000
        required = need(qs.q.groups)
        for slot in qs.slots:
            if slot.status is None and slot.member.max_choices < required:
                slot.status, slot.skip_reason = "skipped", "too_many_choices"
        if not self._quorum_possible(qs, stage):
            return False
        groups = group_options(qs.q.groups)
        budget = self._budget_ms(stage.t0, qs.d1)
        for slot in qs.slots:
            if slot.status is None:
                options = self._order(stage, slot.member, f"{qs.q.name}/groups", groups, True)
                self._launch(qs, stage, slot, 1, None, options, qs.d1, budget, None)
        qs.timers.append(self._spawn_timer(qs.d1, _Timer(stage.index, qs.q.name)))
        return True

    def _order(
        self, stage: StageInfo, member: StageMember, qname: str, options: tuple[OptionSpec, ...], permute: bool
    ) -> tuple[OptionSpec, ...]:
        """Letter order: choice options permuted per (stage profile, member, question); others declared order."""
        if not permute:
            return options
        p = permutation(self.seed, stage.ref, member.member_id, qname, len(options))
        return tuple(options[i] for i in p)

    def _launch(
        self,
        qs: QState,
        stage: StageInfo,
        slot: Slot,
        round_: int,
        group: str | None,
        options: tuple[OptionSpec, ...],
        deadline: float,
        budget_ms: int,
        criteria: tuple[str | None, str | None] | None,
    ) -> None:
        request = member_request(
            slot.member,
            qs.q.instructions,
            options,
            criteria,
            call_kind="verification_member" if self._kind == "verification_member" else "decision_member",
            snapshot=self.ctx.snapshot,
        )
        unit = CallUnit(stage.index, qs.q.name, slot, round_, group, options, request, deadline, budget_ms)
        qs.units.append(unit)
        self._spawn_unit(unit)

    def _drop_unit(self, qs: QState, unit: CallUnit, now: float) -> None:
        unit.done = True
        unit.scope.cancel()
        for rec in unit.calls:
            self.runner.mark_cancelled(rec, unit, now)
        if unit in qs.units:
            qs.units.remove(unit)

    # --- events ----------------------------------------------------------------------------------------------------
    def _handle(self, event: Event) -> None:
        if isinstance(event, _UnitDone):
            self._on_unit(event.unit, event.outcome)
        elif isinstance(event, _Timer):
            self._on_timer(event)
        elif isinstance(event, _StageReady):
            self._on_stage_ready(event.stage)
        elif isinstance(event, _StageFailed):
            for qs in self._eligible():
                if qs.pending is not None:
                    self._escalation_metric(self.stages[qs.stage], qs.pending[1], "ran")
                qs.pending = None
                self._make_final(qs)
            self._progress()
        elif isinstance(event, _StageExpired):
            self._skip_budget(self._eligible())
        elif isinstance(event, _Recheck):
            qs = self.by_name[event.qname]
            self._rechecks.discard(event.qname)
            if qs.stage == event.stage:
                self._check(qs, coalesced=True)
        elif isinstance(event, _S1Due):
            if not self.s1_published and not self.final_published:
                self.s1_published = True
                self._on_s1()
        else:
            self._cancel()

    def _on_unit(self, unit: CallUnit, out: UnitOutcome) -> None:
        if unit.done:
            return
        unit.done = True
        qs = self.by_name[unit.qname]
        if unit in qs.units:
            qs.units.remove(unit)
        slot = unit.slot
        slot.region = out.region or slot.region
        slot.model_revision = out.model_revision or slot.model_revision
        if unit.round == 2:
            self._on_round2(qs, slot, unit, out)
        else:
            slot.status, slot.ended_at, slot.error = out.status, out.ended_at, out.error
            if out.status == "ok" and out.dist is not None:
                if unit.round == 1:
                    gid = {g.label: g.id for g in qs.q.groups}
                    slot.round1 = {gid[k]: p for k, p in out.dist.items()}
                else:
                    slot.dist, slot.unresolved, slot.censored = dict(out.dist), out.unresolved, out.censored
        if out.status == "ok" and unit.stage == 0:
            self._observe_quorum()
        self._check(qs)

    def _on_round2(self, qs: QState, slot: Slot, unit: CallUnit, out: UnitOutcome) -> None:
        if slot.status is not None or unit.group is None:
            return
        if out.status == "ok" and out.dist is not None:
            slot.round2[unit.group] = dict(out.dist)
            slot.pending_groups.discard(unit.group)
            if not slot.pending_groups:
                slot.status, slot.ended_at = "ok", out.ended_at
            return
        slot.status, slot.error, slot.ended_at = out.status, out.error, out.ended_at
        now = self.clock_now
        for other in [u for u in qs.units if u.slot is slot]:  # the member can no longer be ok: stop its calls
            self._drop_unit(qs, other, now)

    def _on_timer(self, event: _Timer) -> None:
        for qs in self.qstates:
            if qs.stage != event.stage or qs.closed or qs.final:
                continue
            if event.qname is None or (qs.q.name == event.qname and qs.round == 1):
                self._close(qs, "timeout")

    # --- closure (spec-06 §7.2) -------------------------------------------------------------------------------------
    def _check(self, qs: QState, *, coalesced: bool = False) -> None:
        if qs.closed or qs.final:
            return
        q_req = self.stages[qs.stage].min_quorum
        required = [s for s in qs.slots if not s.member.cfg.optional and s.status != "skipped"]
        if all(s.status is not None for s in required):  # case 1: every required member is terminal
            self._close(qs, "late")
        elif qcount(s for s in qs.slots if s.status in ("ok", None)) < q_req:  # case 2: quorum impossible
            if coalesced:
                self._close(qs, "late")
            elif qs.q.name not in self._rechecks:
                self._rechecks.add(qs.q.name)
                self._spawn_timer(self.clock_now + COALESCE_S, _Recheck(qs.q.name, qs.stage))

    def _close(self, qs: QState, status: MemberStatus) -> None:
        """Close the current round: in-flight members become `status` (late, or timeout at a deadline)."""
        now = self.clock_now
        for unit in list(qs.units):
            self._drop_unit(qs, unit, now)
        for slot in qs.slots:
            if slot.status is None:
                slot.status, slot.ended_at = status, now
        for timer in qs.timers:
            timer.cancel()
        qs.timers.clear()
        if qs.round == 1:
            self._after_round1(qs, now)
        else:
            self._answer_closed(qs, now)

    def _after_round1(self, qs: QState, now: float) -> None:
        stage = self.stages[qs.stage]
        kind, _ = classify(qs.slots, stage.min_quorum, self.modalities)
        f1 = [s for s in qs.slots if s.status == "ok"]
        if kind != "scored" or self.cancelled:
            for slot in f1:  # computed before it could finish its rounds
                slot.status = "late"
            self._answer_closed(qs, now)
            return
        if stage.deadline - now < MIN_DISPATCH_S:  # round 2 does not fit (R): no call
            for slot in f1:
                slot.status, slot.ended_at = "timeout", now
            self._answer_closed(qs, now)
            return
        pool, _, _ = linear_pool([(s.member.cfg.weight, s.round1 or {}, 0.0) for s in f1])
        sizes = {g.id: len(g.options) for g in qs.q.groups}
        qs.expanded_run, _ = expand(pool, list(sizes), sizes)
        qs.round = 2
        by_id = {g.id: g for g in qs.q.groups}
        budget = self._budget_ms(now, stage.deadline)
        for slot in f1:
            calls = [gid for gid in qs.expanded_run if len(by_id[gid].options) >= 2]
            slot.pending_groups = set(calls)
            if not calls:  # every expanded group has one option: p(o | g) = 1, no call (R)
                continue
            slot.status, slot.ended_at = None, None
            for gid in calls:
                options = self._order(stage, slot.member, f"{qs.q.name}/{gid}", by_id[gid].options, True)
                self._launch(qs, stage, slot, 2, gid, options, stage.deadline, budget, None)
        self._check(qs)

    def _answer_closed(self, qs: QState, now: float) -> None:
        qs.answer = self.build_answer(qs, now)
        qs.closed = True
        qs.pending = None if self.cancelled else self._escalation(qs, now)
        if qs.pending is None:
            self._make_final(qs)
        self._stage_progress(qs.stage, now)
        self._progress()

    def build_answer(self, qs: QState, now: float) -> dict[str, JsonValue]:
        """The answer of the current stage from its slots (pending members count as late: provisional)."""
        stage = self.stages[qs.stage]
        q, q_req, slots = qs.q, stage.min_quorum, qs.slots
        hier = q.method == "hierarchical" and stage.index == 0
        kind, reason = classify(slots, q_req, self.modalities)
        if kind == "scored" and hier and qs.round != 2:
            kind, reason = "unavailable", "no_quorum"  # a hierarchical answer is scored only after round 2
        if kind == "scored":
            if hier:
                return hier_scored(q, stage, slots, q_req, self.actx, now, qs.expanded_run)
            return flat_scored(q, stage, slots, q_req, self.actx, now)
        if hier:
            members = hier_votes(q, slots, qs.expanded_run if qs.round == 2 else [], self.actx, now)
        else:
            members = flat_votes(slots, [o.key for o in q.options], self.actx, now)
        return unscored_answer(
            q, "refusal" if kind == "refusal" else "unavailable", reason, quorum_wire(slots, q_req), members
        )

    def provisional(self, qs: QState, now: float) -> dict[str, JsonValue]:
        """An open answer for S1: the last closed stage answer while escalating; else the current members so far
        (pending `late`); an open hierarchical answer is `unavailable` until it closes (no partial candidates)."""
        if qs.closed or qs.stage > 0:
            assert qs.answer is not None
            return qs.answer
        if qs.q.method != "hierarchical":
            return self.build_answer(qs, now)
        stage = self.stages[0]
        shown = []
        for s in qs.slots:
            done = s.status in ("skipped", "error", "refused", "timeout")
            shown.append(
                Slot(
                    s.member,
                    status=s.status if done else "late",
                    error=s.error,
                    skip_reason=s.skip_reason,
                    calls=s.calls,
                    ended_at=s.ended_at if done else None,
                    region=s.region,
                )
            )
        kind, reason = classify(shown, stage.min_quorum, self.modalities)
        if kind != "unavailable":
            reason = "no_quorum"
        members: list[JsonValue] = [vote_base(s, now, self.actx.region, with_usage=True) for s in shown]
        return unscored_answer(qs.q, "unavailable", reason, quorum_wire(shown, stage.min_quorum), members)

    # --- escalation (spec-06 §9) ------------------------------------------------------------------------------------
    def _budget_limit(self) -> float | None:
        """min(escalate_until, deadline); with `escalate_slack_ms` (turns) the limit is the turn's W counted from
        this run's own t0: `t0 + stage-0 deadline_ms + slack_ms` (spec-04 §3, IMPL-023)."""
        until = self.ctx.escalate_until
        if until is None:
            return None
        slack_ms = self.ctx.escalate_slack_ms
        if slack_ms is None:
            return min(until.at, self.ctx.deadline.at)
        stage0 = self.stages[0]
        return min(stage0.t0 + (stage0.deadline_ms + slack_ms) / 1000, self.ctx.deadline.at)

    def _escalation(self, qs: QState, now: float) -> tuple[DecisionProfileCfg, tuple[str, ...]] | None:
        stage = self.stages[qs.stage]
        esc = stage.profile.escalation
        limit = self._budget_limit()
        if self.req.escalation == "off" or esc is None or limit is None or qs.answer is None:
            return None
        if not filtered_in(qs.q, stage.profile):
            return None
        fired = fired_triggers(qs.answer, esc.when)
        if not fired:
            return None
        try:
            target = self.ctx.snapshot.decision_profile(esc.to_profile)
        except Exception:  # config-validated; a missing target means no escalation
            return None
        if option_count(qs.q.wire) > flat_limit(self.ctx.snapshot, target):
            self._escalation_metric(stage, fired, "skipped_too_many_choices")
            return None
        if now + target.deadline_ms / 1000 > limit:
            self._escalation_metric(stage, fired, "skipped_budget")
            self.budget_skipped = True
            return None
        return target, fired

    def _escalation_metric(self, stage: StageInfo, fired: tuple[str, ...], outcome: str) -> None:
        self.deps.telemetry.instruments.add(
            "aghoo_escalations_total", from_profile=stage.profile.id, trigger=fired[0], outcome=outcome
        )

    def _stage_progress(self, index: int, now: float) -> None:
        mine = [qs for qs in self.qstates if qs.stage == index]
        if index in self.closed_stages or any(not qs.closed for qs in mine):
            return
        self.closed_stages.add(index)
        timer = self._stage_timers.pop(index, None)
        if timer is not None:
            timer.cancel()
        if index > 0:
            self._spans.close()  # the decision.escalation span of this stage
            self._spans = ExitStack()
        eligible = [qs for qs in mine if qs.pending is not None and not qs.final]
        if not eligible:
            return
        stage = self.stages[index]
        target = eligible[0].pending[0] if eligible[0].pending else stage.profile
        limit = self._budget_limit()
        if self.cancelled or limit is None or now + target.deadline_ms / 1000 > limit:
            for qs in eligible:
                if not self.cancelled and qs.pending is not None:
                    self._escalation_metric(stage, qs.pending[1], "skipped_budget")
                    self.budget_skipped = True
                qs.pending = None
                self._make_final(qs)
            return
        triggered = union([qs.pending[1] for qs in eligible if qs.pending is not None])
        assert self._tg is not None
        self._tg.start_soon(self._prepare_stage, target, triggered, limit - target.deadline_ms / 1000)

    def _eligible(self) -> list[QState]:
        return [qs for qs in self.qstates if qs.closed and qs.pending is not None and not qs.final]

    def _skip_budget(self, eligible: list[QState]) -> None:
        """The planned stage no longer fits `min(escalate_until, deadline)`: its answers are final as they are."""
        for qs in eligible:
            if qs.pending is not None:
                self._escalation_metric(self.stages[qs.stage], qs.pending[1], "skipped_budget")
                self.budget_skipped = True
            qs.pending = None
            self._make_final(qs)
        self._progress()

    async def _prepare_stage(self, target: DecisionProfileCfg, triggered: tuple[str, ...], latest: float) -> None:
        """Resolve the target's views, bounded by the stage's latest start `latest` (`_StageExpired` past it)."""
        try:
            members = await stage_members(
                self.ctx.snapshot,
                target,
                self.req.input,
                self.deps.views,
                self.ctx.principal,
                clock=self.deps.clock,
                until=latest,
            )
        except ViewsExpired:
            self.emit(_StageExpired())
            return
        except Exception:  # the eligible answers stay final with their closed stage answers
            _log.exception("escalation stage preparation failed")
            self.emit(_StageFailed())
            return
        now = self.clock_now
        stage = StageInfo(
            index=len(self.stages),
            profile=target,
            members=members,
            t0=now,
            deadline=min(now + target.deadline_ms / 1000, self.ctx.deadline.at),
            deadline_ms=target.deadline_ms,
            min_quorum=target.min_quorum,
            view_signature=input_view_signature([(m.member_id, m.view_kind) for m in members]),
            triggered_by=triggered,
        )
        self.emit(_StageReady(stage))

    def _on_stage_ready(self, stage: StageInfo) -> None:
        eligible = self._eligible()
        if self.cancelled or not eligible:
            return
        now, limit = self.clock_now, self._budget_limit()
        if limit is None or now + stage.deadline_ms / 1000 > limit:  # §9.3 re-check once the views are resolved
            self._skip_budget(eligible)
            return
        for qs in eligible:
            if qs.pending is not None:
                self._escalation_metric(self.stages[qs.stage], qs.pending[1], "ran")
        stage.t0, stage.deadline = now, min(now + stage.deadline_ms / 1000, self.ctx.deadline.at)
        self.stages.append(stage)
        self._spans.enter_context(
            self.deps.telemetry.span(
                "decision.escalation",
                **{
                    "aghoo.escalation.from": self.stages[stage.index - 1].profile.id,
                    "aghoo.escalation.to": stage.profile.id,
                    "aghoo.escalation.questions": ",".join(qs.q.name for qs in eligible),
                },
            )
        )
        self._start(stage, eligible)

    # --- final answers, cancel, publication ---------------------------------------------------------------------
    def _make_final(self, qs: QState) -> None:
        qs.final = True
        if qs.q.method == "hierarchical" and qs.stage == 0 and qs.answer is not None:
            kind = str(qs.answer.get("type"))
            outcome = kind if kind in ("refusal", "unavailable") else "scored"
            reason = str(qs.answer.get("reason") or "")
            self.deps.telemetry.instruments.add(
                "aghoo_decision_hierarchical_total", profile=self.stages[0].profile.id, outcome=outcome, reason=reason
            )

    def _cancel(self) -> None:
        if self.final_published:
            return
        self.cancelled = True
        for qs in self.qstates:
            if qs.final:
                continue
            if not qs.closed:
                self._close(qs, "late")
            else:
                qs.pending = None
                self._make_final(qs)
        self._progress()

    def _progress(self) -> None:
        if self.final_published:
            return
        if all(qs.final for qs in self.qstates):
            self.final_published = True
            if not self.s1_published:
                self.s1_published = True
            self._on_final()
            return
        critical = self.req.questions.commit_critical
        if not self._s1_due and critical and all(self.by_name[n].final for n in critical):
            self._s1_due = True
            self._spawn_timer(self.clock_now + COALESCE_S, _S1Due())

    def _observe_quorum(self) -> None:
        if self._quorum_seen:
            return
        stage = self.stages[0]
        names = self.req.questions.commit_critical or frozenset(self.by_name)
        for name in names:
            qs = self.by_name[name]
            if qs.stage != 0 or qcount(s for s in qs.slots if s.status == "ok") < stage.min_quorum:
                return
        self._quorum_seen = True
        self.deps.telemetry.instruments.observe(
            "aghoo_decision_quorum_reached_seconds", self.clock_now - stage.t0, profile=stage.profile.id
        )
