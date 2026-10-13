"""Per-process overload protection for createTurn, createDecision and the facade (E sprint B; Wave E).

C2b perf smoke section 4: past the knee one api process kept accepting turns until its Postgres and Redis pools
timed out (500s, breakers opening on loop lag) and the goodput halved; nothing bounded the work one process took.
`TurnSlots(cap)` admits a new turn while fewer than `cap` turns of this process are running (the turn service's
registry), between this check and their start, or decisions in flight (createDecision and the facade fan out
through the same pools, Wave E); above it the operation answers 429 `queue_timeout`
(`rate_limit_error`, retryable) with `retry-after: 1` before it touches Postgres, Redis or a model. That is the code
spec-11 §1.1 gives to per-pod capacity exhaustion (stream slots, the fair queue), it is in createTurn's
`x-noema-error-codes`, and SDKs retry it after `retry-after`; 503 `all_routes_unavailable` would claim an upstream
failure. The check runs twice: at the edge, before the middleware stack, for callers whose tenant the key cache
already knows (`noema.api.shed`, Wave E3: a refused request costs almost nothing), and authoritatively in the route
after authentication and the request gate. The cap is shared fairly between tenants, and a tenant saturated by its
own admission limits cannot fill it with waiting turns (`TurnSlots`). A createTurn with an `Idempotency-Key`
found at the cap is not shed before step 7: its duplicate of a running or finished turn attaches or replays without
a slot; only a new keyed turn is then shed (its record is not stored, so the retry runs it).

The cap is NOEMA_MAX_TURNS_PER_PROCESS (default `DEFAULT_MAX_TURNS`; 0 = no cap): per process, so a pod with
`WEB_CONCURRENCY=2` uvicorn workers admits twice that.
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable, Iterable, Mapping
from typing import Final

from noema.core.clock import Clock
from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.telemetry import Instruments

MAX_TURNS_ENV: Final = "NOEMA_MAX_TURNS_PER_PROCESS"
DEFAULT_MAX_TURNS: Final = 120  # overload guard: 240 per 2-process pod, ~1.9x the 125-stream knee (perf/sizing.md)
SHED_MESSAGE: Final = "The server is at capacity; retry shortly."
SHED_RETRY_AFTER_S: Final = 1
SATURATED_S: Final = 2.0  # a tenant stays saturated this long after a turn of it timed out in the admission queue
HELD_METRIC: Final = "aghoo_turn_slots_held"
HELD_EVERY_S: Final = 1.0


def max_turns_from_env(environ: Mapping[str, str] | None = None) -> int | None:
    """The cap from NOEMA_MAX_TURNS_PER_PROCESS (None = no cap); ValueError unless an integer 0..100000."""
    raw = (os.environ if environ is None else environ).get(MAX_TURNS_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_TURNS
    if not raw.isdigit() or int(raw) > 100_000:
        raise ValueError(f"{MAX_TURNS_ENV} must be an integer 0..100000, got {raw!r}")
    return int(raw) or None


class TurnSlots:
    """The admission count of one process (see the module docstring). Sync: the check and the increment happen
    without a suspension point, so concurrent requests cannot overshoot the cap.

    Per tenant (Wave E; max-min since Wave E3): each tenant's share is the max-min fair share of `cap` among the
    tenants holding slots here plus the caller (`fair_share`: tenants holding less than an equal split keep what
    they hold, the rest is split equally among the others; Wave E used `cap // tenants`, which counted a tenant
    holding few slots as if it held a full split). A tenant at or above its share is admitted while fewer than
    `cap` are held; a tenant below its share also within `burst` (`cap // 10`, at least 1) above the cap. So one
    tenant alone can use the whole cap (as before), but it can never keep another tenant out: a newcomer is
    admitted at once, and the freed slots go to the tenants below their share until each has it. The process holds
    at most `cap + burst`.

    Saturated tenants (Wave E3): a tenant one of whose turns timed out in the admission queue here (its own limits,
    e.g. `concurrent_streams_per_project`, held it back for `admission_queue_timeout_ms`) is saturated for
    `SATURATED_S` after the latest such timeout. While saturated it holds at most `burst` pending turn slots
    (admitted createTurns not yet running); further createTurns of it are refused at once (at the edge, before
    authentication) instead of queueing for its own limit, where most would time out after 2 s of ingress, session
    and admission work. Those at most `burst` waiting turns do not count against the other tenants (they wait for
    the tenant's own limits, not for this process; so the process holds at most `cap + burst` plus `burst` per
    saturated tenant). So a tenant flooding past its own limits cannot fill this process's cap with waiting turns;
    its running turns, its decisions and every other tenant are unaffected. The bound is per tenant, not per slot
    set: while one of its models' bulkheads holds `burst` of its turns waiting, its turns for other models are
    refused too (with `retry-after: 1`) until a waiting turn starts or the saturation ends."""

    __slots__ = ("burst", "cap", "pending_turns", "saturated", "shed", "starting", "turns")

    def __init__(self, cap: int | None) -> None:
        self.cap = cap
        self.turns: Mapping[str, object] | None = None  # the turn service's registry (bound by createTurn)
        self.burst = max(1, cap // 10) if cap is not None else 0
        self.starting: dict[str, int] = {}  # per tenant: admitted, not yet counted in the caller's `running`
        self.pending_turns: dict[str, int] = {}  # per tenant: the createTurn slots among `starting`
        self.saturated: dict[str, float] = {}  # per tenant: saturated until (the callers' monotonic clock)
        self.shed = 0  # diagnostics: requests refused since start

    def try_acquire(
        self, tenant: str, running: Mapping[str, int], *, turn: bool = False, now: float | None = None
    ) -> bool:
        """Admit one turn (`turn`; `now` = the monotonic time, for the saturation bound) or decision of `tenant`
        (`running`: this process's started turns per tenant)."""
        if turn and now is not None and self._saturation_refuses(tenant, now):
            return False
        if self.cap is not None and not self._admits(tenant, running, self.cap, now):
            return False
        self.starting[tenant] = self.starting.get(tenant, 0) + 1
        if turn:
            self.pending_turns[tenant] = self.pending_turns.get(tenant, 0) + 1
        return True

    def acquire(self, tenant: str, running: Mapping[str, int], *, turn: bool = False, now: float | None = None) -> None:
        """`try_acquire`, else raise 429 `queue_timeout` (`retry-after: 1`)."""
        if not self.try_acquire(tenant, running, turn=turn, now=now):
            self.shed += 1
            raise NoemaError(ErrorCode.QUEUE_TIMEOUT, SHED_MESSAGE, retry_after_s=SHED_RETRY_AFTER_S)

    def saturate(self, tenant: str, now: float) -> None:
        """A turn of `tenant` timed out in the admission queue: saturated until `now + SATURATED_S`."""
        if self.cap is not None:
            self.saturated[tenant] = now + SATURATED_S

    def _saturation_refuses(self, tenant: str, now: float) -> bool:
        until = self.saturated.get(tenant)
        if until is None:
            return False
        if now >= until:
            del self.saturated[tenant]
            return False
        return self.pending_turns.get(tenant, 0) >= self.burst

    def held(self) -> int:
        """The slots held now: running turns plus admitted ones not yet running (O(tenants))."""
        turns = self.turns
        return (0 if turns is None else len(turns)) + sum(self.starting.values())

    def sheds_early(
        self,
        tenant: Callable[[], str | None],
        running: Callable[[], Mapping[str, int]],
        *,
        turn: bool = False,
        now: float = 0.0,
    ) -> bool:
        """The edge check (`noema.api.shed`, before authentication): True when `try_acquire` would refuse this
        request now. Below the cap with no tenant saturated everyone is admitted (the common case: no caller
        lookup). Else `tenant()` (an unverified hint; None = unknown) is asked: an unknown caller is left to
        authentication and the route's check; a known one is refused by the saturation bound (a turn) or as
        `_admits` decides with `running()`."""
        cap = self.cap
        if cap is None:
            return False
        held = self.held()
        if held < cap and not (turn and self.saturated):
            return False
        who = tenant()
        if who is None:
            return False
        if turn and self._saturation_refuses(who, now):
            return True
        if held < cap:
            return False
        return not self._admits(who, running(), cap, now)

    def release(self, tenant: str, *, turn: bool = False) -> None:
        """The admitted turn (`turn`) started (it is now counted as running), finished or failed before it did; or
        the admitted decision ended."""
        _decrement(self.starting, tenant)
        if turn:
            _decrement(self.pending_turns, tenant)

    def _admits(self, tenant: str, running: Mapping[str, int], cap: int, now: float | None) -> bool:
        used = dict(running)
        for t, n in self.starting.items():
            used[t] = used.get(t, 0) + n
        if now is not None:  # another tenant's waiting turns wait for its own limits, not for this process
            for t, until in self.saturated.items():
                if t != tenant and now < until and t in used:
                    used[t] -= self.pending_turns.get(t, 0)
        total = sum(used.values())
        mine = used.setdefault(tenant, 0)
        if mine < fair_share(used.values(), cap):
            return total < cap + self.burst
        return total < cap


async def export_held(slots: TurnSlots, instruments: Instruments, clock: Clock) -> None:
    """`aghoo_turn_slots_held` = `slots.held()` every `HELD_EVERY_S`, for the life of the process: the 08 §9
    streams-per-pod scale signal (one gauge per process; sum it per pod)."""
    while True:
        instruments.set(HELD_METRIC, slots.held())
        await clock.sleep(HELD_EVERY_S)


def fair_share(holdings: Iterable[int], cap: int) -> float:
    """The max-min fair share of `cap` among tenants holding `holdings` (water filling): the level L with
    sum(min(h, L)) = cap; infinite while the holdings sum to less than `cap` (no tenant is above its share)."""
    values = sorted(holdings)
    remaining = cap
    for i, held in enumerate(values):
        others = len(values) - i  # the tenants holding at least `held`
        if held * others >= remaining:
            return remaining / others
        remaining -= held
    return math.inf


def _decrement(counts: dict[str, int], tenant: str) -> None:
    left = counts.get(tenant, 0) - 1
    if left > 0:
        counts[tenant] = left
    else:
        counts.pop(tenant, None)


def count_by_tenant(tenants: Iterable[str]) -> dict[str, int]:
    """How many of `tenants` (one entry per running turn) each tenant has."""
    out: dict[str, int] = {}
    for t in tenants:
        out[t] = out.get(t, 0) + 1
    return out


WATCHES_PER_PROJECT: Final = 32  # open watchFile streams per project in one process (Wave E2)
WATCH_MESSAGE: Final = "Too many open file event streams for this project; retry shortly."
WATCH_RETRY_AFTER_S: Final = 5


class WatchSlots:
    """The open watchFile streams of one process per (tenant, project) (Wave E2, io CR item 3a: one key could hold
    any number of streams, each re-reading its file row). At `cap` a new stream is 429 `rpm_exceeded` with
    `retry-after` (the only 429 code watchFile declares), before it starts. Sync, like `TurnSlots`."""

    __slots__ = ("cap", "open")

    def __init__(self, cap: int = WATCHES_PER_PROJECT) -> None:
        self.cap = cap
        self.open: dict[tuple[str, str], int] = {}

    def acquire(self, tenant_id: str, project_id: str) -> None:
        """Take a place for one stream of the project, else raise 429 `rpm_exceeded`."""
        key = (tenant_id, project_id)
        held = self.open.get(key, 0)
        if held >= self.cap:
            raise NoemaError(ErrorCode.RPM_EXCEEDED, WATCH_MESSAGE, retry_after_s=WATCH_RETRY_AFTER_S)
        self.open[key] = held + 1

    def release(self, tenant_id: str, project_id: str) -> None:
        """The stream ended (idempotent per acquire)."""
        key = (tenant_id, project_id)
        left = self.open.get(key, 0) - 1
        if left > 0:
            self.open[key] = left
        else:
            self.open.pop(key, None)
