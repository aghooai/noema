"""The turn task (spec-04 §1 step 12 to §11): preflight, the decision run and the generation runs under the commit
rule, late decision policy, verification, persistence and `turn.completed`.

Event order follows spec-04 §4 (IMPL-012). The generation run's events are appended as given, except that
`route.selected` is held until the run's first `generation.started` (it needs that attempt id) and
`route.confirmed` is inserted immediately before `generation.committed`. Per-delta work is a type check, two
attribute updates and the log append (no validation, no copies).

Tasks of one turn: the main flow (preflight -> decision run CM -> the decision driver: S1 with the W cap, then the
single `decision.updated`), the generation driver, the A1 hold timer and one watcher (cancel/abandon, shutdown, the
deadline and the session-lease renewal, whose Postgres call runs in a short task of its own). Every stop path
cancels the active run first, waits for its events, then closes the decision (so generation frames precede the late
decision frames, spec-04 §4.2 G07).
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Final, Literal

import anyio
import anyio.abc

from noema.core.clock import Deadline, deadline_scope
from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.decision import DecisionRun, DecisionSnapshot
from noema.core.protocols.generation import AttemptBudget, AttemptRecord, AttemptSpec, ProfileRun, TurnContext
from noema.core.protocols.services import ServiceSet
from noema.core.values import JsonValue
from noema.turns import persist, session_order
from noema.turns.bindings import Evaluation, Tightening, evaluate, policy_for
from noema.turns.context.budget import profile_usable
from noema.turns.coordination import Coordinator
from noema.turns.finish import finish, turn_status
from noema.turns.model import PreparedTurn, StopStatus, TurnResult
from noema.turns.preflight import Preflight, PreflightInputs, run_preflight
from noema.turns.prompt.builder import PromptInputs, TurnPrompt
from noema.turns.requests import router_request, verification_request
from noema.turns.routing import routing_wire
from noema.turns.spec import Decided, decide, same_spec

if TYPE_CHECKING:
    from noema.core.protocols.events import TurnEventLog

log = logging.getLogger("noema.turns")

GenReason = Literal["client_abort", "deadline"]
_GATE: Final = "gate"
FINAL_GRACE_S: Final = 2.0
COLLECT_S: Final = 2.0  # bound on collecting a run's records after its event loop failed or was cancelled
FINISH_LIMIT_S: Final = 10.0  # bound on finish() when the process runtime cancelled the turn (forced shutdown)
SHUTDOWN_MESSAGE: Final = "The server is shutting down; retry."


class TurnRunner:
    """One turn (see the module docstring). Create, then `await run()` in the turn's own task;
    `request_cancel(status)` is safe from any task."""

    def __init__(
        self,
        services: ServiceSet,
        coordinator: Coordinator,
        prepared: PreparedTurn,
        *,
        on_dispatch: Callable[[], None] = lambda: None,
    ) -> None:
        self.svc = services
        self.coordinator = coordinator
        self.p = prepared
        self.clock = services.clock
        self.snapshot = prepared.snapshot
        self.turn_id = prepared.turn_id
        self.on_dispatch = on_dispatch
        self.events: TurnEventLog = services.events
        spec_cfg = prepared.snapshot.routing.speculation
        self.compare_conditioning = spec_cfg.restart_on_conditioning_change is not False
        self.max_hold_s = None if spec_cfg.max_hold_ms is None else spec_cfg.max_hold_ms / 1000
        self.slack_s = (spec_cfg.slack_ms or 0) / 1000
        self.strategy = prepared.request.strategy
        self.policy: Tightening = policy_for(prepared.store_content)
        self.budget = AttemptBudget()
        self.tctx = TurnContext(
            principal=prepared.principal,
            tenant=prepared.tenant,
            snapshot=prepared.snapshot,
            deadline=prepared.deadline,
            reservation=prepared.reservation,
            turn_id=prepared.turn_id,
            session_id=prepared.session_id,
            trace_id=prepared.trace_id,
        )
        # lifecycle
        self.stop: str | None = None
        self._deferred_error: NoemaError | None = None
        self.cancel_status: StopStatus | None = None  # set by request_cancel: the stop watcher's cancel trigger
        self.shutdown_requested = False  # set by request_shutdown: its shutdown trigger
        self._wake = asyncio.Event()  # the stop watcher's wake-up (cleared before each wait)
        self.gen_done = anyio.Event()
        self.s1_event = anyio.Event()
        self.done = anyio.Event()
        self.result: TurnResult | None = None
        self._preflight_scope: anyio.CancelScope | None = None
        self._verify_scope: anyio.CancelScope | None = None
        # decision state
        self.t_d0: float | None = None
        self.preflight: Preflight | None = None
        self.prompt: TurnPrompt | None = None
        self.drun: DecisionRun | None = None
        self.s1: DecisionSnapshot | None = None
        self.s1_at: float | None = None
        self.decided: Decided | None = None
        self.final: DecisionSnapshot | None = None
        self.final_eval: Evaluation | None = None
        self.router_final: DecisionSnapshot | None = None
        self.verification: DecisionSnapshot | None = None
        self.verification_run: DecisionRun | None = None
        # generation state
        self.runs: list[ProfileRun] = []
        self.active: ProfileRun | None = None
        self.a1: ProfileRun | None = None
        self.restart: Decided | None = None
        self.restarted = False
        self.hold_fired = False
        self.records: list[AttemptRecord] = []
        self.attempts: dict[str, tuple[str, str]] = {}
        self.started_order: list[str] = []
        self._pending_selected: dict[str, JsonValue] | None = None
        self._pending_speculative = True
        self._route_sent = False
        self._s1_applied = False
        self.committed_attempt: str | None = None
        self.committed_run: ProfileRun | None = None
        self.commit_at: float | None = None
        self.commit_wall: datetime | None = None  # the commit on the wall clock of AttemptRecord.first_text_at
        self.commit_asked: datetime | None = None  # when A1 was asked to commit (S1 agrees, or the hold cap)
        self.first_delta_at: float | None = None
        self.text: list[str] = []
        self.completed = False
        self.error: NoemaError | None = None
        self.lease_lost = False
        # the next lease renewal (None: no lease, or it was lost); due RENEW_EVERY_S after ingress (t_in, before the
        # lease was taken at step 10), so a long admission wait cannot let it expire first (Wave E)
        self._renew_due = None if prepared.lease is None else prepared.t_in + session_order.RENEW_EVERY_S
        self._renewing = False
        self._tg: anyio.abc.TaskGroup | None = None  # run()'s task group (the renewal tasks)
        self.finalize_outcome: persist.FinalizeOutcome | None = None  # set by finish() once the finalize ran
        self._created = False
        self.closed = False  # turn.completed was appended

    def emit_created(self) -> None:
        """`turn.created` (spec-04 §1 step 12); idempotent."""
        if self._created:
            return
        self._created = True
        p = self.p
        self.emit(
            {
                "type": "turn.created",
                "turn_id": self.turn_id,
                "session_id": p.session_id,
                "created_at": int(p.created_at.timestamp()),
            }
        )

    # --- events -----------------------------------------------------------------------------------------------------
    def emit(self, event: Mapping[str, JsonValue]) -> None:
        """Append one event to the turn's log."""
        self.events.append(self.turn_id, event)

    def emit_error(self, exc: NoemaError) -> None:
        """The turn's terminal `error` event (at most one per turn)."""
        if self.error is not None:
            return
        self.ensure_route_selected()
        self.error = exc
        self.set_stop("error")
        self.emit(exc.sse_event())

    def ensure_route_selected(self) -> None:
        """P-SSE-1: exactly one `route.selected` per turn, before its error and `turn.completed`. A turn that stops
        before any run started an attempt (preflight, the decision entry, a cancel) gets spec0's."""
        if self._route_sent:
            return
        if self._pending_selected is None:
            self._pending_selected, self._pending_speculative = self.spec0_routing(), self.strategy != _GATE
        self._flush_selected()

    def set_stop(self, reason: str) -> None:
        """Mark the turn as ending: no new generation run starts after this."""
        if self.stop is None:
            self.stop = reason
            self.s1_event.set()  # a gate run waiting for S1 gives up

    @property
    def stopping(self) -> bool:
        """A stop path started (`set_stop`): read through a property, so a check after an await is not narrowed."""
        return self.stop is not None

    @property
    def outcome_final(self) -> bool:
        """The turn already ended with an error, or `generation.completed` was sent: its status is decided."""
        return self.completed or self.error is not None or self.stop == "error"

    def request_cancel(self, status: StopStatus) -> None:
        """POST cancel (`cancelled`), the abandon watchdog (`abandoned`) or a too-slow client (`failed`); the SSE
        reason is always `client_abort` (T18, IMPL-021). Idempotent; the first status wins; a no-op once the
        outcome is final (the status stays `failed` / `completed`, spec-04 §4)."""
        if self.cancel_status is None and not self.done.is_set() and not self.outcome_final:
            self.cancel_status = status
            self._wake.set()

    def request_shutdown(self) -> None:
        """The drain grace ended (spec-11 §7 step 3): stop with `error{all_routes_unavailable, retryable}` (status
        `failed`); a turn whose outcome is final only stops what is left. Idempotent."""
        if not self.done.is_set():
            self.shutdown_requested = True
            self._wake.set()

    @property
    def deadline(self) -> Deadline:
        return self.p.deadline

    def now(self) -> float:
        return self.clock.monotonic()

    # --- routing payloads -------------------------------------------------------------------------------------------
    def spec0_routing(self) -> dict[str, JsonValue]:
        """`route.selected` of A1: spec0, before any decision (served_spec_differs null)."""
        s0 = self.p.spec0
        return routing_wire(self.snapshot, s0.spec, selection=s0.selection, strategy=self.strategy, reason=s0.reason)

    def held_routing(self, route_id: str) -> dict[str, JsonValue]:
        """`route.confirmed` of a hold-cap commit: spec0, decision not yet evaluated."""
        s0 = self.p.spec0
        fallback = route_id != s0.spec.generation_profile
        return routing_wire(
            self.snapshot,
            s0.spec,
            selection="fallback" if fallback else s0.selection,
            strategy=self.strategy,
            reason="holdback_deadline",
            route_id=route_id,
        )

    def matched(self) -> tuple[str, ...]:
        """S1 matches ∪ final matches, ascending (spec-04 §7)."""
        names: set[int] = set()
        if self.decided is not None:
            names.update(self.decided.evaluation.matched)
        if self.final_eval is not None:
            names.update(self.final_eval.matched)
        return tuple(f"bindings[{i}]" for i in sorted(names))

    def decided_routing(
        self, route_id: str, served: AttemptSpec, *, stored: bool = False, conditioning_shown: bool = True
    ) -> dict[str, JsonValue]:
        """The routing once S1 was evaluated. `served` is the spec of the run that served (or tried) the turn; a
        route other than its profile is the IMPL-016 step-3 fallback (selection fallback, differs true)."""
        d = self.decided
        assert d is not None
        assert self.drun is not None
        fallback = route_id != served.generation_profile
        differs = fallback or not same_spec(d.spec, served, include_conditioning=self.compare_conditioning)
        conditioning = d.conditioning if conditioning_shown else None
        reason = f"fallback from {served.generation_profile}: default_profile" if fallback else d.reason
        return routing_wire(
            self.snapshot,
            d.spec,
            selection="fallback" if fallback else d.selection,
            strategy=self.strategy,
            reason=reason,
            decision_id=self.drun.decision_id,
            matched=self.matched() if stored else d.evaluation.matched_names,
            basis=d.basis,
            conditioning=conditioning,
            served_spec_differs=differs,
            route_id=route_id,
        )

    def confirm_routing(self, run: ProfileRun, attempt_id: str, reason: str) -> dict[str, JsonValue]:
        """`route.confirmed`, emitted immediately before the run's `generation.committed` (IMPL-012)."""
        route_id = self.attempts.get(attempt_id, (run.spec.generation_profile, ""))[0]
        if reason == "holdback_deadline" or self.decided is None:
            return self.held_routing(route_id)
        shown = run.spec.conditioning_signature != "none"  # A1 never carries the block (IMPL-014)
        return self.decided_routing(route_id, run.spec, conditioning_shown=shown)

    # --- the turn task ----------------------------------------------------------------------------------------------
    async def run(self) -> TurnResult:
        """Run the turn to `turn.completed` (always emitted, also after a bug: then `error{internal_error}`). When
        the process runtime cancels the task (forced shutdown), the turn still finalizes (shielded, bounded by
        `FINISH_LIMIT_S`) with `error{all_routes_unavailable}`, then the cancellation propagates."""
        p = self.p
        self.emit_created()
        interrupted: BaseException | None = None
        try:
            with self.svc.telemetry.span("turn", **{"aghoo.tenant": p.principal.tenant_id}):
                async with anyio.create_task_group() as tg:
                    self._tg = tg
                    tg.start_soon(self._guarded, self._stop_watch)
                    try:
                        await self._main()
                    finally:
                        self.gen_done.set()
                        tg.cancel_scope.cancel()
        except anyio.get_cancelled_exc_class() as exc:
            interrupted = exc
            log.warning("turn %s interrupted by the process shutdown; finalizing", self.turn_id)
            if not self.outcome_final and self.cancel_status is None:
                self.emit_error(NoemaError(ErrorCode.ALL_ROUTES_UNAVAILABLE, SHUTDOWN_MESSAGE))
        except Exception:
            log.error("turn %s failed unexpectedly", self.turn_id, exc_info=True)
            self.emit_error(NoemaError(ErrorCode.INTERNAL_ERROR, "The turn failed; retry."))
        with anyio.CancelScope(shield=True):
            result = await self._finish(bounded=interrupted is not None)
        if interrupted is not None:
            raise interrupted
        return result

    async def _finish(self, *, bounded: bool) -> TurnResult:
        """`finish()` (shielded by the caller); `done` is set whatever happens."""
        result: TurnResult | None = None
        try:
            if bounded:
                async with deadline_scope(self.clock, self.now() + FINISH_LIMIT_S):
                    await self._final_decision()
                    result = await finish(self)
            else:
                await self._final_decision()
                result = await finish(self)
        except Exception:
            log.error("turn %s: finalize failed", self.turn_id, exc_info=True)
        finally:
            if result is None:
                log.error("turn %s did not finalize", self.turn_id)
                await self._close_unfinished()
                result = TurnResult(status=turn_status(self), error=self.error)
            self.result = result
            self.done.set()
        return result

    async def _close_unfinished(self) -> None:
        """`finish()` failed or ran out of time: still end the stream (`turn.completed`, sealed) and give back the
        reservation, `turn_owner` and, when the finalize did not run (an outboxed one still needs it), the session
        lease (each bounded, best effort; nothing is persisted)."""
        if not self.closed:
            self.closed = True
            usage = persist.usage_sum([persist.usage_wire(rec.usage, rec.cost) for rec in self.records])
            elapsed = round((self.now() - self.p.t_in) * 1000)
            timing: dict[str, JsonValue] = dict.fromkeys(("ttft_ms", "decision_ms", "decision_lead_ms", "holdback_ms"))
            timing.update(e2e_ms=elapsed, spec_restarts=int(self.restarted), wasted_output_tokens=0)
            self.emit({"type": "turn.completed", "turn_id": self.turn_id, "usage": usage, "timings": timing})
            self.events.seal(self.turn_id, self.snapshot.platform.events.resume_window_s)
        steps: list[Callable[[], Awaitable[None]]] = [
            self.p.reservation.release,
            lambda: self.coordinator.delete_owner(self.turn_id),
        ]
        lease = self.p.lease
        if lease is not None and self.finalize_outcome is None:
            steps.append(lambda: session_order.release(self.svc.db, self.coordinator, self.p.principal, lease))
        for step in steps:
            async with deadline_scope(self.clock, self.now() + COLLECT_S):
                try:
                    await step()
                except Exception:
                    log.warning("turn %s: cleanup step failed", self.turn_id, exc_info=True)

    async def _final_decision(self) -> None:
        """The router's final snapshot when `_main` did not reach it (an unexpected failure or a shutdown)."""
        if self.drun is None or self.router_final is not None:
            return
        try:
            self.router_final = await self.drun.finalize()
        except Exception:
            log.warning("turn %s: final decision snapshot unavailable", self.turn_id, exc_info=True)

    async def _guarded(self, watch: Callable[[], Awaitable[None]]) -> None:
        """A watcher task: an unexpected failure stops the turn cleanly (`error{internal_error}`) instead of
        tearing down the turn's task group mid-stream."""
        try:
            await watch()
        except Exception:
            log.error("turn %s: watcher %s failed", self.turn_id, watch.__name__, exc_info=True)
            await self._abort("client_abort", NoemaError(ErrorCode.INTERNAL_ERROR, "The turn failed; retry."))

    async def _main(self) -> None:
        pf = await self._preflight()
        if pf is None or self.stop is not None:
            return
        self.preflight = pf
        self.emit(
            {
                "type": "preflight.completed",
                "modalities": list(pf.modalities),
                "est_input_tokens": pf.est_input_tokens,
                "degradations": [d.to_wire() for d in pf.degradations],
            }
        )
        p = self.p
        self.prompt = TurnPrompt(
            PromptInputs(
                snapshot=self.snapshot,
                principal=p.principal,
                instructions=p.request.instructions,
                history=p.history,
                current=pf.parts,
                plan=pf.plan,
                previous=pf.previous,
                previous_turn_id=pf.previous_turn_id,
                history_labels=pf.history_labels,
                history_texts=pf.history_texts,
            ),
            self.svc.negotiator,
        )
        # escalate_until (W measured from here, at most the W armed at t_d0 below) enables escalation; with
        # escalate_slack_ms the engine counts W from its own t0 (the stage-0 views resolved), the t_d0 read below
        dctx, dreq = router_request(self, pf, escalate_until=self.limit_from(self.now()))
        # holdback/auto: A1 runs spec0 and needs nothing of the decision, so it starts before the decision run's
        # entry and its request leaves while the entry resolves the views and the L2 lookup (E3, ~1.5-2 ms off the
        # TTFT floor; no frame is appended during the entry but A1's, so the spec-04 §4 order is kept); gate waits
        early = self.strategy != _GATE
        async with anyio.create_task_group() as tg:
            if early:
                tg.start_soon(self._generation_task)
            try:
                async with self.svc.decisions.run(dctx, dreq) as drun:
                    # t_d0: the entry returned, so the stage-0 views are resolved (IMPL-023, IMPL-074); W counts here
                    self.t_d0 = self.now()
                    self.drun = drun
                    self.on_dispatch()
                    if self.stopping:  # a cancel, abandon, lease loss or shutdown landed during the entry:
                        await drun.cancel()  # its units drop at their dispatch yields, so no member call goes out
                    if not early:
                        tg.start_soon(self._generation_task)
                    await self._decision_driver(tg)
                    await self.gen_done.wait()
                    self.router_final = await drun.finalize()
                    await self._verify()
            except NoemaError as exc:
                if self.t_d0 is None:  # run().__aenter__: deadline_exceeded before dispatch, tenant_access
                    await self._entry_failed(exc, started=early)
                else:
                    self.emit_error(exc)

    async def _generation_task(self) -> None:
        try:
            await self._generation_driver()
        finally:
            self.gen_done.set()

    async def _entry_failed(self, exc: NoemaError, *, started: bool) -> None:
        """The decision run's entry failed: A1 (started before it) stops first, without a frame for the deadline
        (T19) and with `client_abort` otherwise, so its frames precede the turn's error (spec-04 §4)."""
        if started:
            self.set_stop("error")
            run = self.active
            if run is not None:
                await run.cancel("deadline" if exc.code is ErrorCode.DEADLINE_EXCEEDED else "client_abort")
            await self.gen_done.wait()
        self.emit_error(exc)

    async def _preflight(self) -> Preflight | None:
        p = self.p
        inputs = PreflightInputs(
            principal=p.principal,
            parts=p.request.parts,
            refs=p.refs,
            deadline=p.deadline,
            session_id=p.session_id,
            wait_for_files=p.request.wait_for_files,
            allow_partial_files=p.request.allow_partial_files,
            instructions=p.request.instructions,
            history=p.history,
            budget=p.budget,
            aliases=p.aliases,
        )
        with anyio.CancelScope() as scope, self.svc.telemetry.span("preflight") as span:
            self._preflight_scope = scope
            try:
                return await run_preflight(self.svc.files, self.svc.estimator, self.svc.media_budget, inputs)
            except NoemaError as exc:
                span.error(exc.code.value)
                self.emit_error(exc)
                return None
            finally:
                self._preflight_scope = None
        return None

    # --- the decision -----------------------------------------------------------------------------------------------
    def wait_limit(self) -> float:
        """W = min(t_d0 + router deadline_ms + slack_ms, ctx.deadline) (IMPL-023)."""
        assert self.t_d0 is not None
        return self.limit_from(self.t_d0)

    def limit_from(self, t0: float) -> float:
        """`min(t0 + router deadline_ms + slack_ms, ctx.deadline)`."""
        return min(t0 + self.p.deadline_ms / 1000 + self.slack_s, self.deadline.at)

    def arrived(self) -> Literal["before_generation", "during_holdback", "after_commit"]:
        """`decision.completed.arrived` (spec-04 §3)."""
        if self.strategy == _GATE:
            return "before_generation"
        if self.hold_fired:
            return "after_commit"
        run = self.a1
        return "during_holdback" if run is not None and run.upstream_started else "before_generation"

    async def _decision_driver(self, tg: anyio.abc.TaskGroup) -> None:
        """S1, then the final snapshot (in `_main`'s task). A failure stops the generation task and reaches `run()`
        grouped (`internal_error`), as when this driver was a task of the generation's task group."""
        try:
            await self._decide()
        except Exception as exc:
            tg.cancel_scope.cancel()
            raise BaseExceptionGroup("the decision driver failed", [exc]) from None

    async def _decide(self) -> None:
        drun = self.drun
        assert drun is not None
        s1 = await self._s1_before(drun, self.wait_limit())
        if s1 is None:  # (c): W passed with S1 unresolved -> the closed snapshot
            await drun.cancel()
            s1 = await drun.commit_critical()
        self.s1, self.s1_at = s1, self.now()
        self.emit({"type": "decision.completed", "decision": dict(s1.result), "arrived": self.arrived()})
        if self.cancel_status is None and not self.lease_lost:
            await self._on_s1(s1)
        self.s1_event.set()
        final = await self._final_before(drun, s1)
        if final is not None:
            order = [q.name for q in self.p.questions.questions]
            changed: list[JsonValue] = [n for n in order if n in final.final_names and n not in s1.final_names]
            self.final = final
            self.emit({"type": "decision.updated", "decision": dict(final.result), "changed": changed})
            self._on_final(final)

    async def _final_before(self, drun: DecisionRun, s1: DecisionSnapshot) -> DecisionSnapshot | None:
        """The single final snapshot after a provisional S1. It is bounded by the stage deadline (<= W); an engine
        that has not closed it by `max(W, now) + 2 s` is cancelled (logged), so a turn never hangs on it."""
        if s1.final:
            return None
        updates = drun.updates().__aiter__()
        final: DecisionSnapshot | None = None
        async with deadline_scope(self.clock, max(self.wait_limit(), self.now()) + FINAL_GRACE_S) as scope:
            final = await anext(updates, None)
        if scope.cancelled_caught:
            log.warning("decision of turn %s not final by its deadline; cancelling it", self.turn_id)
            await drun.cancel()
            final = await drun.finalize()
        return final

    async def _s1_before(self, drun: DecisionRun, at: float) -> DecisionSnapshot | None:
        """S1, or None when `at` (W) passes first; cancelling the waiter does not cancel the run."""
        s1: DecisionSnapshot | None = None
        async with deadline_scope(self.clock, at) as scope:
            s1 = await drun.commit_critical()
        return None if scope.cancelled_caught else s1

    async def _on_s1(self, s1: DecisionSnapshot) -> None:
        """Bindings at S1, then (a)/(c): commit when the specs agree, else the one invisible restart."""
        p = self.p
        with self.svc.telemetry.span("bindings.evaluate"):
            decided = decide(
                self.snapshot,
                s1,
                commit_critical=[q.name for q in p.questions.questions if q.name in p.questions.commit_critical],
                request_names={q.name for q in p.questions.questions if q.source == "request"},
                spec0=p.spec0,
                pins=p.request.pins,
                policy=self.policy,
                usable=self._usable,
            )
        self.decided = decided
        if decided.evaluation.tight.spotlight == "strict" and self.prompt is not None:
            self.prompt.strict = True
        await self._apply_s1()

    async def _apply_s1(self) -> None:
        """(a)/(c) on A1, once S1 is decided and A1 is registered (S1 may come first, e.g. while the run's entry
        awaits credentials): commit when the specs agree, else the one invisible restart. Runs at most once."""
        run, decided = self.a1, self.decided
        if run is None or decided is None or self._s1_applied:
            return
        self._s1_applied = True
        if self.strategy == _GATE or self.hold_fired or self.gen_done.is_set() or self.stop:
            return
        if same_spec(decided.spec, run.spec, include_conditioning=self.compare_conditioning):
            self.commit_asked = self.clock.now()
            await run.commit("spec_agrees")  # armed when it has no text yet: commits at its first text (T7)
            return
        if not self._usable(decided.spec.generation_profile):  # the default profile no longer fits (preflight sizes)
            self.set_stop("error")
            self._deferred_error = NoemaError(
                ErrorCode.NO_ROUTE_SATISFIES_CONSTRAINTS, "No route satisfies the constraints."
            )
            await run.cancel("client_abort")  # its frames first; the error follows when the run ended
            return
        self.restart = decided
        await run.cancel("spec_changed")

    def _on_final(self, final: DecisionSnapshot) -> None:
        """The final snapshot: only the post-commit keys of the re-evaluation take effect (IMPL-002, IMPL-018)."""
        ev = evaluate(self.snapshot.routing.bindings, final.answers, final.final_names, self.policy)
        tight = ev.tight
        if self.p.request.pins.verify:
            tight = tight.tighten("verify_answer", True)
        self.final_eval = Evaluation({}, tight, ev.matched)
        if tight.spotlight == "strict" and self.prompt is not None:
            self.prompt.strict = True  # attempts that start from now on

    def _usable(self, profile: str) -> bool:
        tenant = self.snapshot.tenant(self.p.principal.tenant_id)
        est = self.preflight.est_input_tokens if self.preflight is not None else self.p.est_input_tokens
        return profile_usable(
            self.snapshot,
            profile,
            allowed_models=tenant.allowed_models,
            est_input=est,
            spec_max=self.p.request.pins.max_output_tokens,
        )

    def tightening(self) -> Tightening:
        """The turn's effective tightening: S1 ∪ final (spec-04 §7)."""
        tight = self.policy
        if self.decided is not None:
            tight = tight.union(self.decided.evaluation.tight)
        if self.final_eval is not None:
            tight = tight.union(self.final_eval.tight)
        if self.p.request.pins.verify:
            tight = tight.tighten("verify_answer", True)
        return tight

    # --- generation -------------------------------------------------------------------------------------------------
    async def _generation_driver(self) -> None:
        """holdback/auto: A1 on spec0 (held), then at most one restart A2; gate: one run after S1 (spec-04 §3)."""
        p = self.p
        if self.strategy == _GATE:
            await self.s1_event.wait()  # set after S1 was handled, or by any stop
            d = self.decided
            if self.stop is not None or d is None:  # stopped before the gate opened: the route is still owed
                self._pending_selected, self._pending_speculative = self.spec0_routing(), False
                self._flush_selected()
                return
            routing = self.decided_routing(d.spec.generation_profile, d.spec)
            if not self._usable(d.spec.generation_profile):
                self._pending_selected, self._pending_speculative = routing, False
                self._flush_selected()
                self.emit_error(
                    NoemaError(ErrorCode.NO_ROUTE_SATISFIES_CONSTRAINTS, "No route satisfies the constraints.")
                )
                return
            await self._run_profile(d.spec, routing, hold=False, speculative=False, auto_commit="gate", decided=d)
            return
        if self.stopping:  # stopped before A1 started (the decision entry failed at once): no run
            self.ensure_route_selected()
            return
        await self._run_profile(p.spec0.spec, self.spec0_routing(), hold=True, speculative=True, decided=None)
        restart = self.restart
        if self._deferred_error is not None:
            self.emit_error(self._deferred_error)
        if restart is not None and self.stop is None and not self.deadline.expired():
            self.restarted = True
            self.svc.telemetry.instruments.add("aghoo_spec_restarts_total", profile=p.spec0.spec.generation_profile)
            await self._run_profile(restart.spec, None, hold=False, speculative=False, decided=restart)

    async def _run_profile(
        self,
        spec: AttemptSpec,
        selected: dict[str, JsonValue] | None,
        *,
        hold: bool,
        speculative: bool,
        decided: Decided | None,
        auto_commit: Literal["spec_agrees", "gate"] = "spec_agrees",
    ) -> None:
        assert self.prompt is not None
        prompt = self.prompt.for_run(decided.conditioning if decided is not None else None)
        self._pending_selected, self._pending_speculative = selected, speculative
        started: ProfileRun | None = None
        collected = False
        try:
            async with self.svc.generation.run(
                self.tctx,
                spec,
                prompt,
                hold=hold,
                speculative=speculative,
                auto_commit=auto_commit,
                failure_budget=self.budget,
            ) as run:
                started = self.active = run
                self.runs.append(run)
                if hold:
                    self.a1 = run
                if self.stop is not None:  # stopped while the run was starting
                    await run.cancel("deadline" if self.stop == "deadline" else "client_abort")
                elif hold:
                    await self._apply_s1()  # S1 resolved while the run was starting
                ended = interrupted = False
                try:
                    async with anyio.create_task_group() as tg:
                        if hold:
                            tg.start_soon(self._hold_timer, run)
                        async for ev in run.events():
                            self._on_gen_event(run, ev)
                        tg.cancel_scope.cancel()
                    ended = True
                except anyio.get_cancelled_exc_class():
                    interrupted = True  # the engine's exit ends the attempts; collected below
                    raise
                finally:
                    if not interrupted:
                        collected = await self._collect(run, ended=ended)
        except NoemaError as exc:  # run().__aenter__ raises only tenant_access_misconfigured
            self._flush_selected()
            self.emit_error(exc)
        finally:
            if started is not None and not collected:  # after the engine's exit every attempt is terminal
                await self._collect(started, ended=True)
            self.active = None
            self._flush_selected()

    async def _collect(self, run: ProfileRun, *, ended: bool) -> bool:
        """The run's AttemptRecords (usage lines, attempt rows), also when its event loop failed (the run is
        cancelled first); shielded and bounded by `COLLECT_S`. True when collected."""
        records: tuple[AttemptRecord, ...] | None = None
        with anyio.CancelScope(shield=True):
            async with deadline_scope(self.clock, self.now() + COLLECT_S):
                try:
                    if not ended:
                        await run.cancel("client_abort")
                    records = await run.result()
                except Exception:
                    log.error("turn %s: attempt records not collected", self.turn_id, exc_info=True)
        if records is None:
            log.warning("turn %s: attempt records not collected yet", self.turn_id)
            return False
        self.records.extend(records)
        return True

    def _flush_selected(self) -> None:
        """A run that started no attempt still owes its `route.selected` (P-SSE-1: exactly one per turn)."""
        if self._pending_selected is None:
            return
        if self._route_sent:
            self._pending_selected = None
            return
        self._route_sent = True
        attempt_id = self.svc.ids.new("att")
        self.emit(
            {
                "type": "route.selected",
                "attempt_id": attempt_id,
                "routing": self._pending_selected,
                "speculative": self._pending_speculative,
                "degradations": [],
            }
        )
        self._pending_selected = None

    async def _hold_timer(self, run: ProfileRun) -> None:
        """(b): `first_text() + max_hold_ms` with S1 unresolved commits A1 with `holdback_deadline`."""
        first = await run.first_text()
        if first is None or self.max_hold_s is None:
            return
        await self.clock.sleep(max(0.0, first + self.max_hold_s - self.now()))
        if self.s1 is None and self.stop is None and self.committed_attempt is None:
            self.hold_fired = True
            self.commit_asked = self.clock.now()
            await run.commit("holdback_deadline")

    def _on_gen_event(self, run: ProfileRun, ev: Mapping[str, JsonValue]) -> None:
        """Append one run event (hot path: deltas cost a type check and the append)."""
        kind = ev.get("type")
        if kind == "generation.delta":
            if self.first_delta_at is None:
                self.first_delta_at = self.now()
            text = ev.get("text")
            if isinstance(text, str):
                self.text.append(text)
            self.emit(ev)
            return
        attempt_id = str(ev.get("attempt_id", ""))
        if kind == "generation.started":
            self.on_dispatch()  # a model call went out (also when A1 started before the decision entry returned)
            route_id, model = str(ev.get("route_id", "")), str(ev.get("model", ""))
            self.attempts[attempt_id] = (route_id, model)
            self.started_order.append(attempt_id)
            if self._pending_selected is not None and not self._route_sent:
                self._route_sent = True
                degradations = ev.get("degradations")
                self.emit(
                    {
                        "type": "route.selected",
                        "attempt_id": attempt_id,
                        "routing": self._pending_selected,
                        "speculative": self._pending_speculative,
                        "degradations": degradations if isinstance(degradations, list) else [],
                    }
                )
                self._pending_selected = None
        elif kind == "generation.committed":
            self.committed_attempt, self.committed_run, self.commit_at = attempt_id, run, self.now()
            self.commit_wall = self.clock.now()
            routing = self.confirm_routing(run, attempt_id, str(ev.get("reason", "")))
            self.emit({"type": "route.confirmed", "attempt_id": attempt_id, "routing": routing})
        elif kind == "generation.completed":
            self.completed = True
        elif kind == "error":
            if self.error is not None:
                return  # one terminal error per turn
            if self.deadline.expired():  # an attempt stopped by ctx.deadline: the turn's error is T19's
                self.emit_error(NoemaError(ErrorCode.DEADLINE_EXCEEDED, "The turn deadline passed."))
                return
            code = ev.get("code")
            self.ensure_route_selected()
            self.error = NoemaError(ErrorCode(str(code)), str(ev.get("message", "")))
            self.set_stop("error")
        self.emit(ev)

    # --- verification -----------------------------------------------------------------------------------------------
    async def _verify(self) -> None:
        """IMPL-024: after `generation.completed` with non-empty text and the final snapshot; never after an error,
        a cancel or an abandon. Advisory: a verifier failure ends nothing."""
        if not self.completed or self.stop is not None or self.cancel_status is not None or not "".join(self.text):
            return
        if not self.tightening().verify_answer or self.p.verify_questions is None:
            return
        vctx, vreq = verification_request(self)
        final: DecisionSnapshot | None = None
        with anyio.CancelScope() as scope, self.svc.telemetry.span("verification") as span:
            self._verify_scope = scope
            try:
                async with self.svc.decisions.run(vctx, vreq) as vrun:
                    self.verification_run = vrun
                    final = await vrun.finalize()
            except NoemaError as exc:
                span.error(exc.code.value)
                log.warning("verification of turn %s not run: %s", self.turn_id, exc.code.value)
                return
            finally:
                self._verify_scope = None
        if scope.cancelled_caught or final is None:
            return
        self.verification = final
        self.emit({"type": "turn.verified", "verification": dict(final.result)})

    # --- stop paths -------------------------------------------------------------------------------------------------
    async def _abort(self, reason: GenReason, error: NoemaError | None) -> None:
        """Stop the turn: cancel the active run (`client_abort` frames, or none for `deadline`), wait until the
        generation driver is done (its frames come first), then the turn's error (if any) and the decision close."""
        self.set_stop("cancel" if reason == "client_abort" else "deadline")
        if self._preflight_scope is not None:
            self._preflight_scope.cancel()
        if self._verify_scope is not None:
            self._verify_scope.cancel()
        run = self.active
        if run is not None:
            await run.cancel(reason)
        await self.gen_done.wait()
        if error is not None:
            self.emit_error(error)
        if self.drun is not None:
            await self.drun.cancel()

    async def _stop_watch(self) -> None:
        """Cancel/abandon, the shutdown, the turn deadline and the lease renewals in one task (E3; a watcher task
        each before): each stop trigger is handled once, in the order it fires, as its own watcher handled it. A stop
        path in progress ends first; a later trigger then finds the turn stopping, as a concurrent watcher found it.
        A due renewal runs in a short task (`_renew`), so its Postgres call never delays a stop trigger."""
        cancel = shutdown = deadline = True  # triggers not handled yet
        while cancel or shutdown or deadline or self._renew_due is not None:
            if cancel and self.cancel_status is not None:
                cancel = False
                await self._abort("client_abort", None)
            elif shutdown and self.shutdown_requested:
                shutdown = False
                await self._on_shutdown()
            elif deadline and self.now() >= self.deadline.at:
                deadline = False
                await self._on_deadline()
            elif self._renew_due is not None and not self._renewing and self.now() >= self._renew_due:
                self._renewing = True
                assert self._tg is not None
                self._tg.start_soon(self._guarded, self._renew)
            else:
                self._wake.clear()  # no await since the checks above: a request after them sets it again
                at = self.deadline.at if deadline else math.inf
                if self._renew_due is not None and not self._renewing:
                    at = min(at, self._renew_due)
                if at == math.inf:
                    await self._wake.wait()
                else:
                    async with deadline_scope(self.clock, at):
                        await self._wake.wait()

    async def _on_deadline(self) -> None:
        if self.completed or self.error is not None or self.cancel_status is not None:
            return  # the answer is complete or the turn already ends; the rest is bounded by its own deadlines
        await self._abort("deadline", NoemaError(ErrorCode.DEADLINE_EXCEEDED, "The turn deadline passed."))

    async def _on_shutdown(self) -> None:
        final = self.outcome_final or self.cancel_status is not None
        error = None if final else NoemaError(ErrorCode.ALL_ROUTES_UNAVAILABLE, SHUTDOWN_MESSAGE)
        await self._abort("client_abort", error)

    async def _renew(self) -> None:
        """One lease renewal (started by `_stop_watch`); the next is due RENEW_EVERY_S after this one started, or
        right after it if it took longer. A lost lease stops the turn; a failure (a bug) ends the renewals."""
        lease = self.p.lease
        assert lease is not None
        due = self.now() + session_order.RENEW_EVERY_S
        renewed = False
        try:
            renewed = await session_order.renew(self.svc.db, self.p.principal, lease)
        finally:
            self._renew_due = due if renewed else None
            self._renewing = False
            self._wake.set()
        if renewed:
            return
        log.warning("session lease of turn %s lost (taken over after a stall)", self.turn_id)
        self.lease_lost = True
        await self._abort("client_abort", NoemaError(ErrorCode.INTERNAL_ERROR, "The session lease was lost; retry."))
