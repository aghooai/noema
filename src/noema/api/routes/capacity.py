"""The per-process cap of createTurn, createDecision and the facade (`noema.api.overload`, Wave E): one `Slot`
per request, taken before any session, admission or model work. A keyed request found at the cap is not refused
before step 7: its duplicate attaches or replays without a slot (IMPL-077), and only a new one is shed then (its
idempotency record is not stored, so the retry runs it)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from fastapi import Request

from noema.api import deps
from noema.api.overload import TurnSlots, count_by_tenant
from noema.core.errors import ErrorCode, NoemaError
from noema.core.principal import Principal

if TYPE_CHECKING:  # typing only: the decision routes import this module and no other domain package (rule 5)
    from noema.turns.runner import TurnRunner


def running_by_tenant(slots: TurnSlots) -> dict[str, int]:
    """This process's running turns per tenant (none before the first createTurn bound the registry)."""
    turns = cast("Mapping[str, TurnRunner] | None", slots.turns)
    return {} if turns is None else count_by_tenant(r.p.principal.tenant_id for r in turns.values())


class Slot:
    """One request's place under the cap (a no-op when the app has no `turn_slots`). createTurn passes the turn
    service's registry (`turns`), which every later request then counts."""

    __slots__ = ("_slots", "_svc", "_turn", "_who", "held")

    def __init__(
        self,
        request: Request,
        svc: deps.ApiServices,
        who: Principal,
        *,
        keyed: bool,
        turns: Mapping[str, TurnRunner] | None = None,
    ) -> None:
        self._slots: TurnSlots | None = getattr(request.app.state, "turn_slots", None)
        self._svc = svc
        self._who = who
        self._turn = turns is not None  # a createTurn slot (the saturation bound counts these)
        self.held = False
        if self._slots is not None:
            if turns is not None:
                self._slots.turns = turns
            if keyed:
                self.held = self._slots.try_acquire(
                    who.tenant_id, running_by_tenant(self._slots), turn=self._turn, now=svc.clock.monotonic()
                )
            else:
                self._take()

    def claimed(self) -> None:
        """Step 7 returned New (the request will run): a keyed request deferred at the cap is shed now."""
        if self._slots is not None and not self.held:
            self._take()

    def release(self) -> None:
        """Give the place back (idempotent): the turn now counts as running, or the request ended."""
        if self._slots is not None and self.held:
            self.held = False
            self._slots.release(self._who.tenant_id, turn=self._turn)

    def failed(self, exc: BaseException) -> None:
        """The held turn's steps 6-11 raised `exc`: an admission `queue_timeout` (the cap's own refusals happen
        before the slot is held) saturates the tenant (`TurnSlots.saturate`)."""
        inner = exc
        while isinstance(inner, BaseExceptionGroup) and len(inner.exceptions) == 1:
            inner = inner.exceptions[0]
        timed_out = isinstance(inner, NoemaError) and inner.code is ErrorCode.QUEUE_TIMEOUT
        if self._slots is not None and self.held and timed_out:
            self._slots.saturate(self._who.tenant_id, self._svc.clock.monotonic())

    def _take(self) -> None:
        assert self._slots is not None
        try:
            self._slots.acquire(
                self._who.tenant_id, running_by_tenant(self._slots), turn=self._turn, now=self._svc.clock.monotonic()
            )
        except NoemaError:
            self._svc.telemetry.instruments.add("aghoo_turns_shed_total")
            raise
        self.held = True


def keyed(ctx: deps.RequestContextView) -> bool:
    """True when the request carries an Idempotency-Key."""
    return ctx.idempotency is not None and ctx.idempotency.key is not None
