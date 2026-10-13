"""`FakeAdmission` and `FakeReservation` (spec-02 §8, spec-11 §1.2): the GCRA rules of spec-11 §1.2 in memory.

Buckets (all-or-nothing): `key:{key_id}:rpm|tpm` and `prj:{tenant}:{project}:rpm|tpm` (skipped for system
principals), `mdl:{tenant}:{alias}:rpm|tpm` for aliases with `tenants.<t>.limits.model_quotas`. Limits: the tenant
override, else `platform.limits`. Stream slots (`graph.streams`) wait in FIFO order, re-checked on every release
and every 100 ms on the clock, until `queue_timeout_s` -> `queue_timeout`. Rate exhaustion -> 429 at once.
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

import anyio
import anyio.lowlevel

from noema.core.clock import Clock, Deadline
from noema.core.errors import ErrorCode, NoemaError
from noema.core.principal import Principal
from noema.core.protocols.config import ConfigRegistry
from noema.core.protocols.ops import WorkGraph
from noema.core.protocols.telemetry import Instruments
from noema.core.values import CallKind, UsageRecord

TAU_MS: Final = 60_000.0
QUEUE_POLL_S: Final = 0.1
_log: Final = logging.getLogger("tests.fakes.admission")


@dataclass
class _Debit:
    bucket: str
    limit: int
    n: int
    unit: str  # "requests" | "tokens"
    alias: str | None  # mdl buckets only


class LocalGcra:
    """The spec-11 §1.2 GCRA over an in-memory TAT table (ms)."""

    def __init__(self) -> None:
        self.tat: dict[str, float] = {}

    def try_debit(self, debits: list[_Debit], now_ms: float) -> tuple[bool, int, list[str]]:
        """All-or-nothing; returns (allowed, retry_after_s, failing buckets)."""
        failing, retry = [], 0
        new: dict[str, float] = {}
        for d in debits:
            interval = 60_000.0 / d.limit
            tat2 = max(self.tat.get(d.bucket, now_ms), now_ms) + d.n * interval
            if d.n > d.limit:
                failing.append(d.bucket)
                retry = max(retry, 60)
            elif tat2 - now_ms > TAU_MS:
                failing.append(d.bucket)
                retry = max(retry, max(1, math.ceil((tat2 - now_ms - TAU_MS) / 1000)))
            new[d.bucket] = tat2
        if failing:
            return False, retry, failing
        self.tat.update(new)
        return True, 0, []

    def refund(self, bucket: str, limit: int, r: int, now_ms: float) -> None:
        if r > 0 and bucket in self.tat:
            self.tat[bucket] = max(now_ms, self.tat[bucket] - r * 60_000.0 / limit)

    def remaining(self, bucket: str, limit: int, now_ms: float) -> tuple[int, int]:
        """(remaining, reset_s)."""
        busy = max(0.0, self.tat.get(bucket, now_ms) - now_ms)
        return max(0, math.floor((TAU_MS - busy) / (60_000.0 / limit))), math.ceil(busy / 1000)


@dataclass
class _Slots:
    live: set[int] = field(default_factory=set)
    waiters: list[int] = field(default_factory=list)


class FakeReservation:
    """Implements `Reservation` (spec-11 §1.2). `consumed` counts consume() calls per (model, kind)."""

    def __init__(
        self,
        admission: FakeAdmission,
        principal: Principal,
        graph: WorkGraph,
        debits: list[_Debit],
        slot: tuple[str, str, int] | None,
    ) -> None:
        self._admission = admission
        self.principal = principal
        self.graph = graph
        self._debits = debits
        self._slot = slot
        self.consumed: Counter[tuple[str, CallKind]] = Counter()
        self.reconciled = False
        self.released = False
        self._headers = admission._headers(debits)

    def consume(self, kind: CallKind, model: str) -> None:
        self.consumed[(model, kind)] += 1
        if self.consumed[(model, kind)] > self.graph.calls.get((model, kind), 0):
            self._admission._overrun(kind, model)

    async def reconcile(self, usage: Mapping[str, UsageRecord]) -> None:
        await anyio.lowlevel.checkpoint()
        if self.reconciled:
            return
        self.reconciled = True
        actual_total = sum(u.input_tokens + u.output_tokens for u in usage.values())
        self._refund_calls()
        for d in self._debits:
            if d.unit == "tokens":
                actual = (
                    actual_total
                    if d.alias is None
                    else usage.get(d.alias, UsageRecord(0, 0)).input_tokens
                    + (usage.get(d.alias, UsageRecord(0, 0)).output_tokens)
                )
                self._admission._refund(d, max(0, d.n - actual))

    def _refund_calls(self) -> None:
        for d in self._debits:
            if d.unit != "requests":
                continue
            if d.alias is None:
                used = sum(self.consumed.values())
            else:
                used = sum(n for (model, _), n in self.consumed.items() if model == d.alias)
            self._admission._refund(d, max(0, d.n - used))

    def headers(self) -> Mapping[str, str]:
        return dict(self._headers)

    async def release(self) -> None:
        with anyio.CancelScope(shield=True):
            await anyio.lowlevel.checkpoint()
        if self.released:
            return
        self.released = True
        if not self.reconciled:
            self._refund_calls()  # tokens stay debited without a reconcile
        if self._slot is not None:
            self._admission._free(self._slot)


class FakeAdmission:
    """Implements `Admission` (see the module docstring). `env == "test"` turns a reservation overrun into an
    AssertionError (spec-02 §8); `instruments` (optional) receives `aghoo_reservation_overrun_total`."""

    def __init__(
        self, clock: Clock, config: ConfigRegistry, *, env: str = "test", instruments: Instruments | None = None
    ) -> None:
        self._clock = clock
        self._config = config
        self._env = env
        self._instruments = instruments
        self.gcra = LocalGcra()
        self._slots: dict[tuple[str, str], _Slots] = {}
        self._next = 0
        self._changed = anyio.Event()
        self.overruns: list[tuple[CallKind, str]] = []

    def _now_ms(self) -> float:
        return self._clock.monotonic() * 1000.0

    def _debits(self, principal: Principal, graph: WorkGraph) -> list[_Debit]:
        snap = self._config.current()
        plat = snap.platform.limits
        tenant_limits = snap.tenant(principal.tenant_id).limits

        def lim(name: str) -> int:
            override = getattr(tenant_limits, name) if tenant_limits is not None else None
            return int(override if override is not None else getattr(plat, name))

        tokens = graph.tokens_in + graph.tokens_out
        debits: list[_Debit] = []
        if principal.key_id is not None:
            debits += [
                _Debit(f"key:{principal.key_id}:rpm", lim("rpm_per_key"), graph.total_calls, "requests", None),
                _Debit(f"key:{principal.key_id}:tpm", lim("tpm_per_key"), tokens, "tokens", None),
            ]
        if principal.project_id:
            prj = f"prj:{principal.tenant_id}:{principal.project_id}"
            debits += [
                _Debit(f"{prj}:rpm", lim("rpm_per_project"), graph.total_calls, "requests", None),
                _Debit(f"{prj}:tpm", lim("tpm_per_project"), tokens, "tokens", None),
            ]
        quotas = tenant_limits.model_quotas if tenant_limits is not None else {}
        for alias, quota in sorted(quotas.items()):
            n_calls = sum(n for (a, _), n in graph.calls.items() if a == alias)
            if n_calls or graph.tokens_by_model.get(alias):
                mdl = f"mdl:{principal.tenant_id}:{alias}"
                debits += [
                    _Debit(f"{mdl}:rpm", quota.rpm, n_calls, "requests", alias),
                    _Debit(f"{mdl}:tpm", quota.tpm, graph.tokens_by_model.get(alias, 0), "tokens", alias),
                ]
        return [d for d in debits if d.n > 0 or d.unit == "requests"]

    def _headers(self, debits: list[_Debit]) -> dict[str, str]:
        now = self._now_ms()
        out: dict[str, str] = {}
        for unit in ("requests", "tokens"):
            rows = [(self.gcra.remaining(d.bucket, d.limit, now), d.limit) for d in debits if d.unit == unit]
            if rows:
                (remaining, reset), limit = min(rows, key=lambda r: r[0][0])
                out |= {
                    f"x-ratelimit-limit-{unit}": str(limit),
                    f"x-ratelimit-remaining-{unit}": str(remaining),
                    f"x-ratelimit-reset-{unit}": str(reset),  # integer seconds, rounded up (openapi)
                }
        return out

    def _refund(self, debit: _Debit, r: int) -> None:
        self.gcra.refund(debit.bucket, debit.limit, r, self._now_ms())

    def _overrun(self, kind: CallKind, model: str) -> None:
        self.overruns.append((kind, model))
        if self._instruments is not None:
            self._instruments.add("aghoo_reservation_overrun_total", kind=kind)
        _log.error("reservation overrun: kind=%s model=%s", kind, model)
        if self._env == "test":
            raise AssertionError(f"reservation overrun: {kind} on {model}")

    def _free(self, slot: tuple[str, str, int]) -> None:
        tenant, project, sid = slot
        self._slots.get((tenant, project), _Slots()).live.discard(sid)
        self._changed.set()
        self._changed = anyio.Event()

    def live_streams(self, tenant_id: str, project_id: str) -> int:
        return len(self._slots.get((tenant_id, project_id), _Slots()).live)

    async def reserve(
        self, principal: Principal, graph: WorkGraph, *, queue_timeout_s: float, deadline: Deadline | None = None
    ) -> FakeReservation:
        await anyio.lowlevel.checkpoint()
        debits = self._debits(principal, graph)
        ok, retry, failing = self.gcra.try_debit(debits, self._now_ms())
        if not ok:
            code = ErrorCode.RPM_EXCEEDED if any(b.endswith(":rpm") for b in failing) else ErrorCode.TPM_EXCEEDED
            raise NoemaError(code, "Rate limit exceeded.", retry_after_s=retry)
        slot = None
        if graph.streams:
            try:
                slot = await self._take_slot(principal, queue_timeout_s)
            except BaseException:
                for d in debits:
                    self._refund(d, d.n)  # cancellation or timeout while queued leaves nothing debited
                raise
        return FakeReservation(self, principal, graph, debits, slot)

    async def _take_slot(self, principal: Principal, queue_timeout_s: float) -> tuple[str, str, int]:
        key = (principal.tenant_id, principal.project_id)
        cap = self._config.current().platform.limits.concurrent_streams_per_project
        limits = self._config.current().tenant(principal.tenant_id).limits
        if limits is not None and limits.concurrent_streams_per_project is not None:
            cap = limits.concurrent_streams_per_project
        slots = self._slots.setdefault(key, _Slots())
        self._next += 1
        sid = self._next
        slots.waiters.append(sid)
        give_up = self._clock.monotonic() + queue_timeout_s
        try:
            while not (len(slots.live) < cap and slots.waiters[0] == sid):
                if self._clock.monotonic() >= give_up:
                    raise NoemaError(ErrorCode.QUEUE_TIMEOUT, "Admission queue timed out.", retry_after_s=1)
                async with anyio.create_task_group() as tg:  # woken by a release or the 100 ms poll

                    async def poll() -> None:
                        await self._clock.sleep(min(QUEUE_POLL_S, max(0.0, give_up - self._clock.monotonic())))
                        tg.cancel_scope.cancel()

                    tg.start_soon(poll)
                    await self._changed.wait()
                    tg.cancel_scope.cancel()
            slots.live.add(sid)
            return (key[0], key[1], sid)
        finally:
            slots.waiters.remove(sid)


class CountingReservation:
    """A `Reservation` that only records `consume` calls (engine tests that bypass admission)."""

    def __init__(self) -> None:
        self.consumed: list[tuple[CallKind, str]] = []

    def consume(self, kind: CallKind, model: str) -> None:
        self.consumed.append((kind, model))

    async def reconcile(self, usage: Mapping[str, UsageRecord]) -> None:
        await anyio.lowlevel.checkpoint()

    def headers(self) -> Mapping[str, str]:
        return {}

    async def release(self) -> None:
        await anyio.lowlevel.checkpoint()


class FakeRateLimiter:
    """Implements `RateLimiter` (spec-09 §2, spec-11 §1.2): an in-memory GCRA per bucket on the clock. Emission
    interval T = 1 / rate_per_s, tolerance τ = burst · T, so a full bucket admits `burst` units at once; a debit is
    admitted iff max(TAT, now) + n·T - now <= τ, else retry_after_s = the excess (nothing debited)."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self.tat: dict[str, float] = {}  # bucket -> theoretical arrival time (clock seconds)

    def _verdict(self, bucket: str, rate_per_s: float, burst: int, n: int) -> tuple[float, float | None]:
        if rate_per_s <= 0 or burst < 1 or n < 1:
            raise ValueError("rate_per_s > 0, burst >= 1 and n >= 1")
        now = self._clock.monotonic()
        interval = 1.0 / rate_per_s
        tat = max(self.tat.get(bucket, now), now) + n * interval
        excess = tat - now - burst * interval
        return tat, (excess if excess > 1e-9 else None)

    async def check(self, bucket: str, *, rate_per_s: float, burst: int) -> float | None:
        await anyio.lowlevel.checkpoint()
        return self._verdict(bucket, rate_per_s, burst, 1)[1]

    async def debit(self, bucket: str, *, rate_per_s: float, burst: int, n: int = 1) -> float | None:
        await anyio.lowlevel.checkpoint()
        tat, retry = self._verdict(bucket, rate_per_s, burst, n)
        if retry is None:
            self.tat[bucket] = tat
        return retry
