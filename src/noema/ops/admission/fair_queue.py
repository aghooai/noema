"""The per-pod admission queue (spec-11 §1.2): weighted fair queuing over tenants for stream-slot and bulkhead
waits.

Weight 1 per tenant and cost 1 per admission attempt, so the deficit round robin is a plain round robin: each
turn of the rotation lets the next tenant attempt its oldest waiter that is not blocked. FIFO holds within a
tenant: once an attempt finds slot set K full, K is blocked and no later waiter needing K is attempted until K is
released on this pod (`released`) or the 100 ms poll tick (which also catches releases on other pods). A flooding
tenant therefore costs another tenant at most one attempt per rotation, and blocked waiters cost nothing.

The queue has no task of its own: one waiter at a time (the leader) runs the pump while it waits, and hands it on
when its own outcome is in. Attempts are shielded, so a cancelled leader never abandons a half-done reservation;
a success whose waiter is gone is rolled back.
"""

from __future__ import annotations

import heapq
from collections import Counter, deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Final

import anyio
import anyio.lowlevel

from noema.core.clock import Clock

POLL_S: Final = 0.1


class QueueTimeout(Exception):
    """The waiter's queue timeout passed before it was admitted."""


@dataclass(frozen=True, slots=True)
class Full:
    """An attempt's answer when slot set `key` is full: keep waiting."""

    key: str


Attempt = Callable[[], Awaitable[object]]  # returns Full(key) to keep waiting, or the final value; raising = final
Rollback = Callable[[object], Awaitable[None]]  # undoes a final value nobody can take any more


@dataclass(eq=False, slots=True)
class _Waiter:
    tenant: str
    keys: frozenset[str]
    give_up: float
    attempt: Attempt
    rollback: Rollback
    wake: anyio.Event = field(default_factory=anyio.Event)
    queued: bool = True
    done: bool = False
    value: object = None
    error: BaseException | None = None


class FairQueue:
    """See the module docstring. `on_depth` receives the queue depth after every change (the gauge)."""

    def __init__(self, clock: Clock, *, poll_s: float = POLL_S, on_depth: Callable[[int], None] | None = None) -> None:
        self._clock = clock
        self._poll_s = poll_s
        self._on_depth = on_depth
        self._tenants: dict[str, deque[_Waiter]] = {}
        self._rotation: deque[str] = deque()
        self._keys: Counter[str] = Counter()
        self._blocked: set[str] = set()
        self._deadlines: list[tuple[float, int, _Waiter]] = []
        self._seq = 0
        self._size = 0
        self._pumping = False
        self._kick = anyio.Event()
        self._next_tick = 0.0

    def __len__(self) -> int:
        return self._size

    def blocks(self, keys: Iterable[str]) -> bool:
        """True when a queued waiter needs one of `keys` (a newcomer must queue behind it: FIFO)."""
        return any(k in self._keys for k in keys)

    def released(self, keys: Iterable[str]) -> None:
        """Slots of `keys` were freed on this pod: unblock them and wake the pump."""
        for k in keys:
            self._blocked.discard(k)
        self._kick.set()

    async def wait(
        self,
        tenant: str,
        keys: frozenset[str],
        timeout_s: float,
        attempt: Attempt,
        rollback: Rollback,
        *,
        full: str | None = None,
    ) -> object:
        """Queue until `attempt` returns a final value (returned) or raises (re-raised), or `timeout_s` passes
        (`QueueTimeout`). `full`: a key the caller just found full (blocked until released or the next tick).
        Cancellation removes the waiter; a value it can no longer take is rolled back (shielded)."""
        waiter = _Waiter(tenant, keys, self._clock.monotonic() + timeout_s, attempt, rollback)
        if full is not None:
            self._blocked.add(full)
        self._push(waiter)
        try:
            while not waiter.done:
                if not self._pumping:
                    await self._lead(waiter)
                else:
                    waiter.wake = anyio.Event()
                    await waiter.wake.wait()
            await anyio.lowlevel.checkpoint_if_cancelled()  # a cancel that arrived with the value: roll it back
        except BaseException:
            if waiter.done and waiter.error is None:
                with anyio.CancelScope(shield=True):
                    await waiter.rollback(waiter.value)
            elif not waiter.done:
                self._remove(waiter)
            raise
        finally:
            if not self._pumping:
                self._hand_over()
        if waiter.error is not None:
            raise waiter.error
        return waiter.value

    # --- the pump (run by the leader) ---------------------------------------------------------------------------
    async def _lead(self, me: _Waiter) -> None:
        self._pumping = True
        start = self._clock.monotonic()
        if self._next_tick <= start:  # a fresh pump: keys found full just now stay blocked until the first tick
            self._next_tick = start + self._poll_s
        try:
            while True:
                now = self._clock.monotonic()
                self._expire(now)
                if me.done:
                    break
                if now >= self._next_tick:
                    self._blocked.clear()  # the poll: slots may have been freed on another pod
                    self._next_tick = now + self._poll_s
                waiter = self._next()
                if waiter is None:
                    await self._idle(now)
                else:
                    await self._attempt(waiter)
        finally:
            self._pumping = False

    def _hand_over(self) -> None:
        """Wake the oldest waiter of the next tenant: it becomes the leader (called once the old one left)."""
        for tenant in self._rotation:
            self._tenants[tenant][0].wake.set()
            break

    def _next(self) -> _Waiter | None:
        """Round robin over tenants: the next tenant's oldest waiter whose keys are all unblocked."""
        for _ in range(len(self._rotation)):
            tenant = self._rotation[0]
            self._rotation.rotate(-1)
            for waiter in self._tenants[tenant]:
                if waiter.keys.isdisjoint(self._blocked):
                    return waiter
        return None

    async def _attempt(self, waiter: _Waiter) -> None:
        with anyio.CancelScope(shield=True):
            try:
                value = await waiter.attempt()
            except Exception as exc:  # a final error for that waiter (e.g. rate exceeded while queued)
                self._finish(waiter, None, exc)
                return
            if isinstance(value, Full):
                self._blocked.add(value.key)
            elif not waiter.queued:  # cancelled while its attempt ran
                await waiter.rollback(value)
            else:
                self._finish(waiter, value, None)

    async def _idle(self, now: float) -> None:
        """Sleep until a release, a new waiter, the next poll tick or the earliest queue timeout."""
        wake_at = self._next_tick
        if self._deadlines:
            wake_at = min(wake_at, self._deadlines[0][0])
        self._kick = kick = anyio.Event()
        async with anyio.create_task_group() as tg:

            async def timer() -> None:
                await self._clock.sleep(max(0.0, wake_at - now))
                tg.cancel_scope.cancel()

            tg.start_soon(timer)
            await kick.wait()
            tg.cancel_scope.cancel()

    def _expire(self, now: float) -> None:
        while self._deadlines and self._deadlines[0][0] <= now:
            _, _, waiter = heapq.heappop(self._deadlines)
            if waiter.queued:
                self._finish(waiter, None, QueueTimeout())

    # --- bookkeeping --------------------------------------------------------------------------------------------
    def _push(self, waiter: _Waiter) -> None:
        fifo = self._tenants.get(waiter.tenant)
        if fifo is None:
            fifo = self._tenants[waiter.tenant] = deque()
            self._rotation.append(waiter.tenant)
        fifo.append(waiter)
        self._keys.update(waiter.keys)
        self._seq += 1
        heapq.heappush(self._deadlines, (waiter.give_up, self._seq, waiter))
        self._resize(1)
        self._kick.set()

    def _remove(self, waiter: _Waiter) -> None:
        if not waiter.queued:
            return
        waiter.queued = False
        fifo = self._tenants[waiter.tenant]
        fifo.remove(waiter)
        if not fifo:
            del self._tenants[waiter.tenant]
            self._rotation.remove(waiter.tenant)
        self._keys.subtract(waiter.keys)
        for k in waiter.keys:
            if self._keys[k] <= 0:
                del self._keys[k]
        if not self._tenants:
            self._deadlines.clear()
        self._resize(-1)

    def _finish(self, waiter: _Waiter, value: object, error: BaseException | None) -> None:
        self._remove(waiter)
        waiter.value, waiter.error, waiter.done = value, error, True
        waiter.wake.set()

    def _resize(self, delta: int) -> None:
        self._size += delta
        if self._on_depth is not None:
            self._on_depth(self._size)
