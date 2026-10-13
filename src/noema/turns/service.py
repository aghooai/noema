"""`TurnService`: the orchestrator's process-wide entry point for the turn routes (spec-04 §1, §11).

One per process (`TurnService.of(services)` = `services.runtime.local("turns.service", ...)`). It owns the running
turn registry, the `turn_owner` entries (TTL 60 s, refreshed every 20 s), the `turn_cancel` listener and the
abandon watchdog (every 5 s: no subscriber for `resume_window_s` -> cancel, status `abandoned`). The background
tasks start with the first turn (or `start_background()`, which the composition root may call at startup).
"""

from __future__ import annotations

import heapq
import logging
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC
from typing import TYPE_CHECKING, Final, Literal

import anyio

from noema.core.clock import Deadline, deadline_scope
from noema.core.errors import ErrorCode, NoemaError
from noema.core.principal import Principal
from noema.core.protocols.auth import Action, ResourceRef
from noema.core.protocols.services import ServiceSet
from noema.core.protocols.storage import OutboxRecord, UnitOfWork
from noema.core.values import JsonValue
from noema.turns import ingress, session_order
from noema.turns.coordination import CoordinationDown, Coordinator, RedisCoordinator
from noema.turns.model import PreparedTurn, TurnStatus
from noema.turns.request import TurnRequest
from noema.turns.runner import TurnRunner
from noema.turns.views import stored_turn_object

if TYPE_CHECKING:
    from noema.core.protocols.config import ConfigSnapshot

log = logging.getLogger("noema.turns")

LOCAL_KEY: Final = "turns.service"
OWNER_REFRESH_S: Final = 20.0
WATCHDOG_S: Final = 5.0
SHUTDOWN_STOP_S: Final = 5.0  # after the drain grace: the stopped turns' finalize (the drain hook waits for it)
CancelAnswer = Literal["cancelling", "cancelled", "completed"]


@dataclass(frozen=True, slots=True)
class FinishedTurn:
    """A turn that ended on this pod, kept for its resume window (cancel answers for `store: false` turns)."""

    status: TurnStatus
    tenant_id: str
    project_id: str
    principal_id: str
    session_owner_principal_id: str | None
    expires_at: float

    def row(self) -> FinishedRow:
        """As a plain tuple of strings and a float: the GC untracks it, so the resume window's finished turns cost a
        full collection nothing (E3; the dataclass instance was one tracked object per turn)."""
        return (
            self.status,
            self.tenant_id,
            self.project_id,
            self.principal_id,
            self.session_owner_principal_id,
            self.expires_at,
        )


FinishedRow = tuple[TurnStatus, str, str, str, str | None, float]


def _answer(status: TurnStatus) -> CancelAnswer:
    return "cancelled" if status in ("cancelled", "abandoned") else "completed"


class TurnService:
    """See the module docstring."""

    def __init__(self, services: ServiceSet, coordinator: Coordinator | None = None) -> None:
        self.svc = services
        self.coordinator: Coordinator = coordinator or RedisCoordinator(services.redis, services.keys, services.clock)
        self.running: dict[str, TurnRunner] = {}
        self.finished: dict[str, FinishedRow] = {}  # FinishedTurn.row() per turn id
        self._expiry: list[tuple[float, str]] = []  # (expires_at, turn_id) min-heap over `finished`
        self.json_waiters: set[str] = set()
        self._background = False

    @classmethod
    def of(cls, services: ServiceSet) -> TurnService:
        """The process's service (created on first use)."""
        return services.runtime.local(LOCAL_KEY, lambda: cls(services))

    # --- background ------------------------------------------------------------------------------------------------
    def start_background(self) -> None:
        """Start the cancel listener, the owner refresh and the abandon watchdog (once; needs an open runtime)."""
        if self._background:
            return
        self._background = True
        runtime = self.svc.runtime
        runtime.spawn("turns.cancel_listener", lambda: self.coordinator.listen(self.on_remote_cancel))
        runtime.spawn("turns.owner_refresh", self._owner_refresh)
        runtime.spawn("turns.watchdog", self._watchdog)
        runtime.on_drain("turns.drain", self._drain)

    async def on_remote_cancel(self, tenant_id: str, turn_id: str) -> None:
        """A `turn_cancel` message: cancel the local turn of that tenant (messages for another tenant are dropped
        with a `pubsub_tenant_mismatch` security event, spec-09 §7 SEC-ISO-02)."""
        runner = self.running.get(turn_id)
        if runner is None:
            return
        if runner.p.principal.tenant_id == tenant_id:
            runner.request_cancel("cancelled")
        else:
            self._tenant_mismatch(runner.p.principal.tenant_id, turn_id)

    def _tenant_mismatch(self, tenant_id: str, turn_id: str) -> None:
        """spec-09 §1 security event: one content-free log record, the counter and an `audit_event` row (outbox) of
        the local turn's tenant."""
        log.warning(
            "security name=pubsub_tenant_mismatch tenant_id=%s turn_id=%s kind=pubsub_tenant_mismatch",
            tenant_id,
            turn_id,
            extra={"event": "security", "code": "pubsub_tenant_mismatch", "tenant": tenant_id},
        )
        svc = self.svc
        svc.telemetry.instruments.add("aghoo_security_violations_total", kind="pubsub_tenant_mismatch")
        audit_id = svc.ids.new("aud")
        payload: dict[str, JsonValue] = {
            "tenant_id": tenant_id,
            "id": audit_id,
            "actor_type": "system",
            "actor_id": "system:turns",
            "action": "security.pubsub_tenant_mismatch",
            "resource_kind": "turn",
            "resource_id": turn_id,
            "details": {"channel": "turn_cancel"},
            "request_id": None,
            "at": svc.clock.now().astimezone(UTC).isoformat().replace("+00:00", "Z"),
        }
        svc.outbox.put(OutboxRecord(kind="audit_event", id=audit_id, tenant_id=tenant_id, payload=payload))

    async def _owner_refresh(self) -> None:
        while True:
            await self.svc.clock.sleep(OWNER_REFRESH_S)
            await self.coordinator.refresh_owners({tid: self._owner(r.p) for tid, r in self.running.items()})

    async def _watchdog(self) -> None:
        while True:
            await self.svc.clock.sleep(WATCHDOG_S)
            await self.check_abandoned()

    async def check_abandoned(self) -> None:
        """One watchdog pass over the local turns (JSON requests still waiting are attached)."""
        for turn_id, runner in list(self.running.items()):
            if turn_id in self.json_waiters:
                continue
            idle = await self.svc.events.idle_seconds(turn_id)
            if idle is not None and idle > runner.snapshot.platform.events.resume_window_s:
                log.info("turn %s abandoned after %.0f s without a subscriber", turn_id, idle)
                runner.request_cancel("abandoned")

    async def _drain(self, grace_s: float) -> None:
        """Shutdown (spec-11 §7 step 3): running turns continue for the grace period, then stop with
        `error{all_routes_unavailable, retryable}`; returns once they finalized (at most `SHUTDOWN_STOP_S` more),
        so `Outbox.flush` runs after their usage lines and finalize records."""
        clock = self.svc.clock
        deadline = clock.monotonic() + grace_s
        while self.running and clock.monotonic() < deadline:
            await clock.sleep(min(0.25, max(0.0, deadline - clock.monotonic())))
        runners = list(self.running.values())
        for runner in runners:
            runner.request_shutdown()
        async with deadline_scope(clock, clock.monotonic() + SHUTDOWN_STOP_S) as scope:
            for runner in runners:
                await runner.done.wait()
        if scope.cancelled_caught:
            log.error("%d turns not finalized at shutdown", sum(not r.done.is_set() for r in runners))

    def _owner(self, p: PreparedTurn) -> dict[str, JsonValue]:
        return {
            "pod": self.svc.settings.pod_id,
            "tenant_id": p.principal.tenant_id,
            "project_id": p.principal.project_id,
            "principal_id": p.principal.principal_id,
            "session_owner_principal_id": p.session.owner_principal_id if p.session is not None else None,
        }

    # --- createTurn --------------------------------------------------------------------------------------------------
    async def authorize(self, principal: Principal, request: TurnRequest) -> ingress.Authorized:
        """Step 6 (`ingress.authorize`)."""
        return await ingress.authorize(self.svc, principal, request)

    async def prepare(
        self,
        *,
        snapshot: ConfigSnapshot,
        principal: Principal,
        request: TurnRequest,
        authorized: ingress.Authorized,
        turn_id: str,
        t_in: float,
        deadline: Deadline,
        trace_id: str,
    ) -> PreparedTurn:
        """Steps 8-11 (`ingress.prepare`) on the snapshot pinned at ingress."""
        return await ingress.prepare(
            self.svc,
            self.coordinator,
            snapshot=snapshot,
            principal=principal,
            request=request,
            authorized=authorized,
            turn_id=turn_id,
            t_in=t_in,
            deadline=deadline,
            trace_id=trace_id,
        )

    async def create[R](
        self,
        *,
        snapshot: ConfigSnapshot,
        principal: Principal,
        request: TurnRequest,
        turn_id: str,
        t_in: float,
        deadline: Deadline,
        trace_id: str,
        begin: Callable[[], Awaitable[R | None]],
        keyed: bool,
    ) -> PreparedTurn | R:
        """createTurn's steps 6-11 (`authorize`, step 7 = `begin()`, `prepare`): `begin`'s non-None result (an
        idempotent replay or attach) is returned as is. A `foldable` request without an Idempotency-Key (`keyed`)
        runs steps 6-10 in one unit of work (E sprint A: one BEGIN/set_config/COMMIT before the generation
        request instead of three): the session read, the lease and the history read, committed before admission
        (step 11). The error order is the unfolded one; step 9's `broker.get` is made before the unit opens."""
        if keyed or not ingress.foldable(request):
            authorized = await self.authorize(principal, request)
            replay = await begin()
            if replay is not None:
                return replay
            return await self.prepare(
                snapshot=snapshot,
                principal=principal,
                request=request,
                authorized=authorized,
                turn_id=turn_id,
                t_in=t_in,
                deadline=deadline,
                trace_id=trace_id,
            )
        self.svc.authz.check(principal, "turn.create")  # 403 before any lookup (spec-09 §3), STS included
        credentials_error = await ingress.early_credentials(self.svc, principal)
        async with AsyncExitStack() as unit:
            uow = await unit.enter_async_context(self.svc.db.uow(principal))
            authorized = await ingress.authorize_in(self.svc, principal, request, uow)
            replay = await begin()
            if replay is not None:
                return replay
            return await ingress.prepare(
                self.svc,
                self.coordinator,
                snapshot=snapshot,
                principal=principal,
                request=request,
                authorized=authorized,
                turn_id=turn_id,
                t_in=t_in,
                deadline=deadline,
                trace_id=trace_id,
                held=ingress.Held(uow, unit.aclose, credentials_error),
            )

    async def start(self, prepared: PreparedTurn, *, on_dispatch: Callable[[], None] = lambda: None) -> TurnRunner:
        """Step 12: register the running turn (local registry and `turn_owner`) and start its task, which outlives
        the request (a dropped connection is not a cancel). `turn.created` is appended before this returns. Any
        failure here gives back what ingress took (reservation, owner entry, session lease), so a retry is not
        `session_busy`."""
        try:
            self.start_background()
            runner = TurnRunner(self.svc, self.coordinator, prepared, on_dispatch=on_dispatch)
            self.running[prepared.turn_id] = runner
            await self.coordinator.set_owner(prepared.turn_id, self._owner(prepared))
            runner.emit_created()

            async def run() -> None:
                try:
                    await runner.run()
                finally:
                    self._finished(runner)

            self.svc.runtime.spawn(f"turn:{prepared.turn_id}", run)
        except BaseException:  # the registration failed, or the runtime is closed (shutdown)
            self.running.pop(prepared.turn_id, None)
            with anyio.CancelScope(shield=True):
                await prepared.reservation.release()
                await self.coordinator.delete_owner(prepared.turn_id)
                if prepared.lease is not None:
                    await session_order.release(self.svc.db, self.coordinator, prepared.principal, prepared.lease)
            raise
        return runner

    def _finished(self, runner: TurnRunner) -> None:
        p = runner.p
        self.running.pop(p.turn_id, None)
        self.json_waiters.discard(p.turn_id)
        status = runner.result.status if runner.result is not None else "failed"
        now = self.svc.clock.monotonic()
        expiry = self._expiry
        while expiry and expiry[0][0] <= now:  # amortized O(log n) per finished turn
            _, old_id = heapq.heappop(expiry)
            entry = self.finished.get(old_id)
            if entry is not None and FinishedTurn(*entry).expires_at <= now:
                del self.finished[old_id]
        expires_at = now + p.snapshot.platform.events.resume_window_s
        heapq.heappush(expiry, (expires_at, p.turn_id))
        self.finished[p.turn_id] = FinishedTurn(
            status=status,
            tenant_id=p.principal.tenant_id,
            project_id=p.principal.project_id,
            principal_id=p.principal.principal_id,
            session_owner_principal_id=p.session.owner_principal_id if p.session is not None else None,
            expires_at=expires_at,
        ).row()

    async def wait(self, runner: TurnRunner) -> None:
        """Wait for a `stream: false` turn (the JSON client counts as attached for the watchdog)."""
        self.json_waiters.add(runner.turn_id)
        try:
            await runner.done.wait()
        finally:
            self.json_waiters.discard(runner.turn_id)

    # --- getTurn / cancelTurn ----------------------------------------------------------------------------------------
    def _check_turn(
        self,
        principal: Principal,
        action: Action,
        owner: str | None,
        project: str | None,
        session_owner: str | None,
        turn_id: str,
    ) -> None:
        """The spec-09 §3 resource rules of a turn (anything invisible, or a cancel by a non-creator, is 404)."""
        ref = ResourceRef("turn", turn_id, project, owner, session_owner)
        self.svc.authz.check(principal, action, ref)

    async def get(self, principal: Principal, turn_id: str) -> dict[str, JsonValue]:
        """getTurn: the stored turn (404 until persisted; running turns are not readable in Phase 1)."""
        self.svc.authz.check(principal, "turn.read")
        async with self.svc.db.uow(principal) as uow:
            row = await uow.turns.get(turn_id)
            if row is None:
                raise NoemaError(ErrorCode.NOT_FOUND, "Turn not found.")
            session_owner = await _session_owner(uow, row)
            self._check_turn(
                principal, "turn.read", _s(row.get("principal_id")), _s(row.get("project_id")), session_owner, turn_id
            )
            attempts = await uow.attempts.list_for_turn(turn_id)
            decision = await _decision(uow, row.get("decision_id"))
            verification = await _decision(uow, row.get("verification_decision_id"))
        return stored_turn_object(row, attempts, decision, verification)

    async def cancel(self, principal: Principal, turn_id: str) -> dict[str, JsonValue]:
        """cancelTurn (spec-04 §11): a running turn here or on another pod -> `cancelling`; a finished turn ->
        `cancelled` or `completed`; unknown or foreign -> 404; Redis down and the turn not local and not stored ->
        500 (retryable)."""
        self.svc.authz.check(principal, "turn.cancel")
        runner = self.running.get(turn_id)
        if runner is not None:
            p = runner.p
            if p.principal.tenant_id != principal.tenant_id:  # another tenant's turn: invisible (spec-09 §3)
                raise NoemaError(ErrorCode.NOT_FOUND, "Turn not found.")
            self._check_turn(
                principal,
                "turn.cancel",
                p.principal.principal_id,
                p.principal.project_id,
                p.session.owner_principal_id if p.session else None,
                turn_id,
            )
            if runner.done.is_set() and runner.result is not None:
                return {"turn_id": turn_id, "status": _answer(runner.result.status)}
            if runner.outcome_final and runner.cancel_status is None:  # ended with an error or the full answer
                return {"turn_id": turn_id, "status": "completed"}
            runner.request_cancel("cancelled")
            return {"turn_id": turn_id, "status": "cancelling"}
        kept = self.finished.get(turn_id)
        done = FinishedTurn(*kept) if kept is not None else None
        if done is not None and done.expires_at > self.svc.clock.monotonic() and done.tenant_id == principal.tenant_id:
            self._check_turn(
                principal, "turn.cancel", done.principal_id, done.project_id, done.session_owner_principal_id, turn_id
            )
            return {"turn_id": turn_id, "status": _answer(done.status)}
        redis_down = False
        try:
            owner = await self.coordinator.get_owner(turn_id)
        except CoordinationDown:
            owner, redis_down = None, True
        if owner is not None and owner.get("tenant_id") == principal.tenant_id:
            self._check_turn(
                principal,
                "turn.cancel",
                _s(owner.get("principal_id")),
                _s(owner.get("project_id")),
                _s(owner.get("session_owner_principal_id")),
                turn_id,
            )
            try:
                await self.coordinator.publish_cancel(principal.tenant_id, turn_id)
            except CoordinationDown as exc:
                raise NoemaError(ErrorCode.INTERNAL_ERROR, "The turn can't be cancelled right now; retry.") from exc
            return {"turn_id": turn_id, "status": "cancelling"}
        async with self.svc.db.uow(principal) as uow:
            row = await uow.turns.get(turn_id)
            session_owner = await _session_owner(uow, row) if row is not None else None
        if row is not None:
            self._check_turn(
                principal, "turn.cancel", _s(row.get("principal_id")), _s(row.get("project_id")), session_owner, turn_id
            )
            status = str(row.get("status", "completed"))
            return {"turn_id": turn_id, "status": "cancelled" if status in ("cancelled", "abandoned") else "completed"}
        if redis_down:
            raise NoemaError(ErrorCode.INTERNAL_ERROR, "The turn can't be looked up right now; retry.")
        raise NoemaError(ErrorCode.NOT_FOUND, "Turn not found.")


def _s(value: object) -> str | None:
    return value if isinstance(value, str) else None


async def _session_owner(uow: UnitOfWork, row: Mapping[str, object]) -> str | None:
    session_id = row.get("session_id")
    if not isinstance(session_id, str):
        return None
    session = await uow.sessions.get(session_id)
    return _s(session.get("owner_principal_id")) if session is not None else None


async def _decision(uow: UnitOfWork, decision_id: object) -> Mapping[str, object] | None:
    return await uow.decisions.get(decision_id) if isinstance(decision_id, str) else None
