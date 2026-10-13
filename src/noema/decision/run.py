"""`DecisionRunImpl`: the `DecisionRun` of one decision (spec-06 §10, §14, §16; IMPL-002, IMPL-020, IMPL-022,
IMPL-070, IMPL-071).

`commit_critical()` returns S1 once every commit-critical answer is final (`quorum` while a non-blocking answer is
still open); `updates()` yields at most the final snapshot; `finalize()` records the usage lines, puts the result
into L2 and, outside turns, inserts the row (outbox on a DB failure or a stall past the budget) and offers a stored
row to labeling, all shielded with a 2 s budget so a client abort never loses them.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final

import anyio
import orjson

from noema.core.clock import deadline_scope
from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.decision import DecisionContext, DecisionRequest, DecisionSnapshot
from noema.core.protocols.labeling import ContentRef
from noema.core.protocols.ops import UsageLine
from noema.core.protocols.storage import OutboxRecord
from noema.core.values import ContentPolicy, JsonValue, UsageLineName, UsageRecord

from .coordinator import Coordinator
from .deps import EngineDeps
from .repo import decision_row, outbox_payload
from .snapshot import build_result, response_view, status_of
from .state import StageInfo
from .summary import summarize
from .usage import UsageSum

_log: Final = logging.getLogger("noema.decision.run")
FINALIZE_BUDGET_S: Final = 2.0


def line_id(text: str) -> str:
    """`h(x) = sha256(x).hexdigest()[:26]`: deterministic usage-line ids (spec-11 §2)."""
    return hashlib.sha256(text.encode()).hexdigest()[:26]


@dataclass(frozen=True, slots=True)
class _Settled:
    """What `finalize()` and `usage_by_alias()` read of the coordinator, taken when the final snapshot is published:
    every unit is closed by then (its call records and the members' statuses no longer change) and the coordinator
    handles no event after it. The run then drops the coordinator (E3): its slots, call records, units and streams
    were ~300 tracked objects kept to the turn's end, and the run <-> coordinator callbacks made them cyclic garbage
    that only a full collection frees."""

    lines: tuple[tuple[UsageLineName, str, str, UsageSum], ...]  # (line, Bedrock id, region, sum), first-call order
    by_alias: tuple[tuple[str, UsageRecord], ...]
    statuses: tuple[tuple[str, str], ...]  # (member model alias, status) per slot, for the member metric
    l2_ok: bool  # neither budget-skipped nor cancelled, and no metadata_only view


def _settle(coord: Coordinator, ctx: DecisionContext) -> _Settled:
    normal: UsageLineName = "verification" if ctx.purpose == "verification" else "decision"
    sums: dict[tuple[UsageLineName, str], tuple[UsageSum, str]] = {}
    by_alias: dict[str, UsageRecord] = {}
    for _, slot in coord.all_slots:
        for rec in slot.calls:
            if rec.outcome not in ("completed", "cancelled"):
                continue
            name: UsageLineName = normal if rec.outcome == "completed" else "waste"
            acc, region = sums.get((name, rec.model_id), (UsageSum(), rec.region or ctx.tenant.home_region))
            acc.add(rec.usage, rec.cost)
            sums[(name, rec.model_id)] = (acc, region)
            alias = rec.model_alias
            by_alias[alias] = by_alias[alias] + rec.usage if alias in by_alias else rec.usage
    metadata_only = any(m.view_kind == "metadata_only" for st in coord.stages for m in st.members)
    return _Settled(
        lines=tuple((name, model_id, region, acc) for (name, model_id), (acc, region) in sums.items()),
        by_alias=tuple(by_alias.items()),
        statuses=tuple((slot.member.cfg.model, slot.status or "late") for _, slot in coord.all_slots),
        l2_ok=not coord.budget_skipped and not coord.cancelled and not metadata_only,
    )


class DecisionRunImpl:
    """Implements `DecisionRun` (see the module docstring). Built by `DecisionEngineImpl.run`."""

    def __init__(
        self,
        deps: EngineDeps,
        ctx: DecisionContext,
        req: DecisionRequest,
        *,
        decision_id: str,
        digest: str,
        seed: int,
        started_at: datetime,
        t_start: float,
        stage0: StageInfo,
        l2_key: str | None,
        hit: Mapping[str, JsonValue] | None = None,
        fill_usage: UsageSum | None = None,
    ) -> None:
        self.deps, self.ctx, self.req = deps, ctx, req
        self._id, self._digest, self._seed = decision_id, digest, seed
        self._started_at, self._t_start = started_at, t_start
        self.stage0 = stage0
        self.l2_key = l2_key
        self._hit = hit
        self._fill_usage = fill_usage
        self._s1: DecisionSnapshot | None = None
        self._final: DecisionSnapshot | None = None
        self._full: dict[str, JsonValue] | None = None
        self._s1_event = anyio.Event()
        self._final_event = anyio.Event()
        self._finalized: DecisionSnapshot | None = None
        self._finalize_lock = anyio.Lock()
        self.failure: BaseException | None = None
        self._failure_recorded = False
        self._row_inserted = False
        self.coordinator: Coordinator | None = None  # dropped once the final snapshot is published (`_settled`)
        self._settled: _Settled | None = None
        self._dispatched = False
        if hit is not None:
            self._publish_hit(hit)

    @property
    def decision_id(self) -> str:
        return self._id

    # --- publication (called by the coordinator, sync) ------------------------------------------------------------
    def attach(self, coordinator: Coordinator) -> None:
        self.coordinator = coordinator
        self._dispatched = True

    def publish_s1(self) -> None:
        self._s1 = self._snapshot(final=False)
        self._observe("quorum")
        self._s1_event.set()

    def publish_final(self) -> None:
        final = self._snapshot(final=True)
        self._final = final
        if self._s1 is None:
            self._s1 = final
            self._observe("quorum")
        self._observe("all")
        coord = self.coordinator
        assert coord is not None
        self._settled = _settle(coord, self.ctx)
        self.coordinator = None  # see `_Settled`: nothing reads it after the final snapshot
        self._s1_event.set()
        self._final_event.set()

    def _settled_now(self) -> _Settled | None:
        """The settled summary; after a coordinator bug (no final snapshot) taken from the coordinator as it is."""
        if self._settled is not None:
            return self._settled
        return _settle(self.coordinator, self.ctx) if self.coordinator is not None else None

    def fail(self, exc: BaseException) -> None:
        """The coordinator crashed (a bug): waiters raise 500 internal_error instead of hanging."""
        self.failure = exc
        self._s1_event.set()
        self._final_event.set()

    def _observe(self, stage: str) -> None:
        elapsed = self.deps.clock.monotonic() - self.stage0.t0
        self.deps.telemetry.instruments.observe(
            "aghoo_decision_seconds", elapsed, profile=self.stage0.profile.id, stage=stage, member=""
        )

    def _snapshot(self, *, final: bool) -> DecisionSnapshot:
        coord = self.coordinator
        assert coord is not None
        now = self.deps.clock.monotonic()
        answers = [
            qs.answer if qs.final and qs.answer is not None else coord.provisional(qs, now) for qs in coord.qstates
        ]
        full = build_result(
            decision_id=self._id,
            req=self.req,
            trace_id=self.ctx.trace_id,
            digest=self._digest,
            seed=self._seed,
            stages=coord.stages,
            closed_stages=coord.closed_stages,
            all_slots=coord.all_slots,
            answers=answers,
            final=final,
            started_at=self._started_at,
            elapsed_s=now - self._t_start,
        )
        finals = frozenset(qs.q.name for qs in coord.qstates if qs.final)
        return self._make(full, final=final, final_names=finals, cache_hit=False)

    def _make(
        self, full: dict[str, JsonValue], *, final: bool, final_names: frozenset[str], cache_hit: bool
    ) -> DecisionSnapshot:
        answers = full.get("answers")
        listed = [a for a in answers if isinstance(a, dict)] if isinstance(answers, list) else []
        status = status_of(listed, final)
        if final:
            self._full = full
        row = None
        if final and self.ctx.store and self.ctx.turn_id is not None:
            row = self._row(full, status, self.ctx.content_policy, cache_hit)
        return DecisionSnapshot(
            decision_id=self._id,
            status=status,
            final=final,
            final_names=final_names,
            answers={str(a.get("name")): summarize(a, final=str(a.get("name")) in final_names) for a in listed},
            result=response_view(full, self.req.include),
            row=row,
        )

    def _row(
        self, full: Mapping[str, JsonValue], status: str, policy: ContentPolicy, cache_hit: bool
    ) -> dict[str, object]:
        return decision_row(
            self.ctx,
            self.req,
            decision_id=self._id,
            created_at=self._started_at,
            status=status,
            result=full,
            cache_hit=cache_hit,
            policy=policy,
            redact=self.deps.telemetry.redact,
        )

    def _publish_hit(self, hit: Mapping[str, JsonValue]) -> None:
        names = frozenset(q.name for q in self.req.questions.questions)
        final = self._make(dict(hit), final=True, final_names=names, cache_hit=True)
        self._s1 = self._final = final
        self._s1_event.set()
        self._final_event.set()

    def _raise_failure(self) -> None:
        if self.failure is not None:
            raise NoemaError(ErrorCode.INTERNAL_ERROR, "The decision failed unexpectedly.")

    # --- DecisionRun -------------------------------------------------------------------------------------------------
    async def commit_critical(self) -> DecisionSnapshot:
        await self._s1_event.wait()
        self._raise_failure()
        assert self._s1 is not None
        return self._s1

    async def updates(self) -> AsyncIterator[DecisionSnapshot]:
        s1 = await self.commit_critical()
        if s1.final:
            return
        await self._final_event.wait()
        self._raise_failure()
        assert self._final is not None
        yield self._final

    async def cancel(self) -> None:
        if self._final_event.is_set():
            return
        if self.coordinator is not None:
            self.coordinator.cancel()
        await self._final_event.wait()

    def row_for(self, content_policy: ContentPolicy) -> Mapping[str, object] | None:
        final = self._final
        if final is None or final.row is None or self._full is None:
            return None
        return self._row(self._full, final.status, content_policy, self._hit is not None)

    async def finalize(self) -> DecisionSnapshot:
        await self._final_event.wait()
        self._raise_failure()
        assert self._final is not None
        with anyio.CancelScope(shield=True):
            async with self._finalize_lock:
                if self._finalized is None:
                    clock, final = self.deps.clock, self._final
                    row = self._row_to_insert(final)
                    async with deadline_scope(clock, clock.monotonic() + FINALIZE_BUDGET_S) as scope:
                        await self._side_effects(final, row)
                    if scope.cancelled_caught:
                        _log.error("decision finalize exceeded its %.0f s budget", FINALIZE_BUDGET_S)
                    if row is not None:  # sync, after the budget: a stalled store can no longer drop the row
                        self._settle_row(final, row)
                    self._finalized = final
        assert self._finalized is not None
        return self._finalized

    def record_failure_usage(self) -> None:
        """After a coordinator bug (`failure` set): the usage lines of the calls already made, once; no row."""
        if self.failure is None or self._failure_recorded:
            return
        self._failure_recorded = True
        try:
            self._record_usage()
        except Exception:
            _log.exception("decision failure: usage lines failed")

    def usage_by_alias(self) -> dict[str, UsageRecord]:
        """Actual usage per model alias (completed and estimated cancelled calls), for `Reservation.reconcile`."""
        settled = self._settled_now()
        return {} if settled is None else dict(settled.by_alias)

    @property
    def full_result(self) -> Mapping[str, JsonValue] | None:
        """The final result with members (the row's `result`), whatever `include` asked for."""
        return self._full

    @property
    def dispatched(self) -> bool:
        """True iff the run made model calls (an L2 hit does not)."""
        return self._dispatched

    # --- side effects --------------------------------------------------------------------------------------------
    def _row_to_insert(self, final: DecisionSnapshot) -> dict[str, object] | None:
        """The row `finalize()` writes: only outside turns, when stored, for a dispatched or cached result."""
        ctx = self.ctx
        if ctx.turn_id is not None or not ctx.store or self._full is None:
            return None
        return self._row(self._full, final.status, ctx.content_policy, self._hit is not None)

    async def _side_effects(self, final: DecisionSnapshot, row: dict[str, object] | None) -> None:
        """Usage lines and metrics (sync), the L2 put, then the row insert (`_row_inserted` once committed)."""
        for name, effect in (("usage lines", self._record_usage), ("member metrics", self._member_metrics)):
            try:
                effect()
            except Exception:  # a collaborator bug never fails a finished decision
                _log.exception("decision finalize: %s failed", name)
        try:
            await self._l2_put(final)
        except Exception:
            _log.exception("decision finalize: L2 put failed")
        if row is None:
            return
        try:
            async with self.deps.db.uow(self.ctx.principal) as uow:
                await uow.decisions.insert(row)
            self._row_inserted = True
        except Exception as exc:  # never fails the request: write-behind (IMPL-022), queued by _settle_row
            _log.warning("decision row insert failed (%s)", type(exc).__name__)

    def _settle_row(self, final: DecisionSnapshot, row: dict[str, object]) -> None:
        """A row that is not committed (failed, or abandoned at the budget: rolled back) goes to the outbox, whose
        apply is ON CONFLICT DO NOTHING; a committed row is offered to labeling (spec-06 §16: never before the row
        exists)."""
        ctx = self.ctx
        if not self._row_inserted:
            _log.warning("decision row not inserted within finalize; queued in the outbox")
            self.deps.outbox.put(OutboxRecord("decision_row", self._id, ctx.principal.tenant_id, outbox_payload(row)))
            return
        if ctx.purpose in ("decision", "router") and row.get("input") is not None:
            try:
                self.deps.sampler.offer(ctx.tenant.tenant_id, final, ContentRef(self._id, None))
            except Exception:
                _log.exception("labeling offer failed")

    def _line(
        self, line_id_: str, line: UsageLineName, model_id: str | None, region: str | None, acc: UsageSum
    ) -> UsageLine:
        ctx = self.ctx
        return UsageLine(
            id=line_id_,
            tenant_id=ctx.principal.tenant_id,
            project_id=ctx.principal.project_id,
            line=line,
            model_id=model_id,
            region=region,
            usage=acc.usage,
            cost=acc.cost,
            turn_id=ctx.turn_id,
            decision_id=self._id,
            session_id=ctx.session_id,
        )

    def _record_usage(self) -> None:
        """One line per (decision, line, Bedrock id): completed calls on decision/verification, cancelled calls on
        waste; an L2 hit records only the avoided fill-time cost (cache_savings)."""
        if self._hit is not None:
            fill = self._fill_usage or UsageSum()
            self.deps.cost.record(self._line(line_id(f"{self._id}:cache_savings"), "cache_savings", None, None, fill))
            return
        settled = self._settled_now()
        if settled is None:
            return
        for name, model_id, region, acc in settled.lines:
            self.deps.cost.record(self._line(line_id(f"{self._id}:{name}:{model_id}"), name, model_id, region, acc))

    def _member_metrics(self) -> None:
        settled = self._settled_now()
        if settled is None:
            return
        for member, status in settled.statuses:
            self.deps.telemetry.instruments.add("aghoo_decision_member_status_total", member=member, status=status)

    async def _l2_put(self, final: DecisionSnapshot) -> None:
        settled = self._settled_now()
        if self.l2_key is None or self._hit is not None or settled is None or self._full is None:
            return
        # not completed, budget-skipped, capped, cancelled (closed early with members in flight, also a pending
        # escalation: truncated) or with a metadata_only view: no L2 entry
        if final.status != "completed" or not settled.l2_ok or self.stage0.capped:
            return
        ttl = self.ctx.snapshot.platform.decision_cache.ttl_s
        await self.deps.cache.put(self.l2_key, orjson.dumps(self._full), ttl)
