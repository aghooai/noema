"""`Admission` and `Reservation` over GCRA buckets in Redis (spec-11 §1.2, IMPL-078); model quotas are sliding-window
buckets (`gcra.quota_bucket`, Wave E2 QL-2).

`reserve` debits the whole worst-case work graph at once: rate exhaustion is a 429 at once (`rpm_exceeded` if any
RPM bucket fails, else `tpm_exceeded`, retry-after = the largest wait among the failing buckets; nothing is
debited), also for a request that would queue behind earlier waiters; a full stream-slot set or bulkhead waits in
the per-pod fair queue up to `queue_timeout_s` (`queue_timeout`, retry-after 1). Labeling (best effort) never
queues: short of bucket or slot headroom it is `queue_timeout` at once. `Reservation.consume` is a sync counter
decrement; `reconcile` refunds unused calls and tokens, `release` frees the slots (and, before any reconcile,
refunds the unused calls: whichever comes first refunds them, once); both are idempotent and shielded.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

import anyio
import anyio.lowlevel

from noema.core.clock import Clock, Deadline
from noema.core.errors import ErrorCode, NoemaError
from noema.core.principal import Principal
from noema.core.protocols.config import ConfigRegistry
from noema.core.protocols.ops import WorkGraph
from noema.core.protocols.telemetry import Instruments
from noema.core.redis_keys import RedisKeys
from noema.core.values import CallKind, UsageRecord
from noema.ops.admission.fair_queue import POLL_S, FairQueue, Full, QueueTimeout
from noema.ops.admission.gcra import (
    Bucket,
    Verdict,
    denied,
    remaining,
    reset_s,
    retry_after_s,
    wall_ms,
    window_reset_s,
)
from noema.ops.admission.plan import Plan, build_plan
from noema.ops.admission.stores import PROBE_INTERVAL_S, REDIS_TIMEOUT_S, Debit, FailoverStore, RedisStore

if TYPE_CHECKING:
    import redis.asyncio

RELEASE_SHIELD_S: Final = 1.0
_log: Final = logging.getLogger("noema.ops.admission")
_MESSAGES: Final = {
    ErrorCode.RPM_EXCEEDED: "Too many model calls per minute for this key, project or model quota.",
    ErrorCode.TPM_EXCEEDED: "Too many tokens per minute for this key, project or model quota.",
    ErrorCode.QUEUE_TIMEOUT: "No capacity became free in time; retry shortly.",
}


class AdmissionRejected(NoemaError):
    """A 429 from `reserve`. `headers` are the x-ratelimit-* headers of the buckets as they stand (nothing was
    debited); the openapi 429 response may carry them (`deps.add_error_headers`)."""

    def __init__(self, code: ErrorCode, *, retry_after_s: int, headers: Mapping[str, str]) -> None:
        super().__init__(code, _MESSAGES[code], retry_after_s=retry_after_s)
        self.headers: Mapping[str, str] = headers


def rate_headers(buckets: Sequence[Bucket], verdict: Verdict, *, debited: bool) -> dict[str, str]:
    """The six x-ratelimit-* headers: per unit, the bucket with the lowest remaining (IMPL-033), after the debit
    (`debited`) or as the buckets stand."""
    now_ms = verdict.now_ms
    values = verdict.after if debited else verdict.before
    out: dict[str, str] = {}
    for unit in ("requests", "tokens"):
        best: tuple[int, int] | None = None
        for i, bucket in enumerate(buckets):
            if bucket.unit == unit:
                left = remaining(bucket, values[i], now_ms)
                if best is None or left < best[0]:
                    best = (left, i)
        if best is not None:
            left, i = best
            bucket = buckets[i]
            if bucket.slices:
                reset = window_reset_s(bucket, verdict.resets[i], now_ms, debited=debited)
            else:
                reset = reset_s(values[i], now_ms)
            out[f"x-ratelimit-limit-{unit}"] = str(bucket.limit)
            out[f"x-ratelimit-remaining-{unit}"] = str(left)
            out[f"x-ratelimit-reset-{unit}"] = str(reset)
    return out


def rejection(plan: Plan, verdict: Verdict, debit: Debit) -> AdmissionRejected:
    """The 429 of a denied debit (spec-11 §1.2): labeling short of headroom is `queue_timeout`."""
    now = verdict.now_ms
    headers = rate_headers(debit.buckets, verdict, debited=False)
    if plan.labeling:
        return AdmissionRejected(ErrorCode.QUEUE_TIMEOUT, retry_after_s=1, headers=headers)
    rows = zip(debit.buckets, verdict.after, verdict.waits, strict=True)
    failing = [(b, after, wait) for b, after, wait in rows if denied(b, after, now)]
    code = ErrorCode.RPM_EXCEEDED if any(b.unit == "requests" for b, _, _ in failing) else ErrorCode.TPM_EXCEEDED
    retry = max(retry_after_s(b, after, now, wait) for b, after, wait in failing)
    return AdmissionRejected(code, retry_after_s=retry, headers=headers)


def queue_timeout() -> AdmissionRejected:
    return AdmissionRejected(ErrorCode.QUEUE_TIMEOUT, retry_after_s=1, headers={})


class GcraReservation:
    """Implements `Reservation` (spec-11 §1.2). `consume` and `headers` are sync and in memory (IMPL-078)."""

    __slots__ = (
        "_admission",
        "_by_alias",
        "_calls_refunded",
        "_consumed",
        "_debit",
        "_headers",
        "_left",
        "_plan",
        "_reconciled",
        "_released",
    )

    def __init__(self, admission: GcraAdmission, plan: Plan, verdict: Verdict, debit: Debit) -> None:
        self._admission = admission
        self._plan = plan
        self._debit = debit
        self._left: dict[tuple[str, CallKind], int] = dict(plan.graph.calls)
        self._by_alias: dict[str, int] = {}
        self._consumed = 0
        self._headers = MappingProxyType(rate_headers(debit.buckets, verdict, debited=True))
        self._reconciled = False
        self._released = False
        self._calls_refunded = False  # set by whichever of reconcile / release refunds the unused calls first

    @property
    def graph(self) -> WorkGraph:
        """The reserved work graph."""
        return self._plan.graph

    def consume(self, kind: CallKind, model: str) -> None:
        key = (model, kind)
        left = self._left.get(key, 0)
        self._left[key] = left - 1
        self._consumed += 1
        self._by_alias[model] = self._by_alias.get(model, 0) + 1
        if left <= 0:
            self._admission._overrun(kind, model)

    async def reconcile(self, usage: Mapping[str, UsageRecord]) -> None:
        if self._reconciled:
            await anyio.lowlevel.checkpoint()
            return
        self._reconciled = True
        calls_done, self._calls_refunded = self._calls_refunded, True
        total = sum(u.input_tokens + u.output_tokens for u in usage.values())
        refunds: list[Bucket] = []
        for bucket in self._debit.buckets:
            if bucket.unit == "requests":
                if calls_done:  # release() already gave the unused calls back
                    continue
                used = self._consumed if bucket.alias is None else self._by_alias.get(bucket.alias, 0)
            elif bucket.alias is None:
                used = total
            else:
                actual = usage.get(bucket.alias)
                used = 0 if actual is None else actual.input_tokens + actual.output_tokens
            if bucket.n > used:
                refunds.append(bucket.with_n(bucket.n - used))
        with anyio.move_on_after(RELEASE_SHIELD_S, shield=True):
            await self._admission._refund(self._debit, refunds, release=False)

    def headers(self) -> Mapping[str, str]:
        return self._headers

    async def release(self) -> None:
        if self._released:
            await anyio.lowlevel.checkpoint()
            return
        self._released = True
        refunds = [] if self._calls_refunded else self._unused_calls()
        self._calls_refunded = True
        with anyio.move_on_after(RELEASE_SHIELD_S, shield=True):
            await self._admission._refund(self._debit, refunds, release=True)

    def _unused_calls(self) -> list[Bucket]:
        out: list[Bucket] = []
        for bucket in self._debit.buckets:
            if bucket.unit == "requests":
                used = self._consumed if bucket.alias is None else self._by_alias.get(bucket.alias, 0)
                if bucket.n > used:
                    out.append(bucket.with_n(bucket.n - used))
        return out


class GcraAdmission:
    """Implements `Admission` (spec-11 §1.2) on Redis/Valkey, with the per-pod local fail-safe at
    `limit // expected_pods` (`stores.FailoverStore`) and the per-pod fair queue (`fair_queue.FairQueue`).

    `redis=None` keeps every bucket in process memory (single-process dev and tests). Limits are read from
    `config.current()` at each reserve. `env == "test"` turns a reservation overrun into an AssertionError.
    `instruments`: `aghoo_admission_rejections_total{code}` per 429 of `reserve`, `aghoo_reservation_overrun_total`,
    `aghoo_queue_depth` and the `FailoverStore` metrics."""

    def __init__(
        self,
        clock: Clock,
        config: ConfigRegistry,
        keys: RedisKeys,
        redis: redis.asyncio.Redis | None,
        *,
        env: str,
        instruments: Instruments | None = None,
        expected_pods: int = 1,
        redis_timeout_s: float = REDIS_TIMEOUT_S,
        probe_interval_s: float = PROBE_INTERVAL_S,
        poll_s: float = POLL_S,
    ) -> None:
        self._clock = clock
        self._config = config
        self._keys = keys
        self._env = env
        self._instruments = instruments
        primary = RedisStore(redis, keys, timeout_s=redis_timeout_s) if redis is not None else None
        self._store = FailoverStore(
            primary, clock, expected_pods=expected_pods, probe_interval_s=probe_interval_s, instruments=instruments
        )
        self._queue = FairQueue(clock, poll_s=poll_s, on_depth=self._depth)

    @property
    def degraded(self) -> bool:
        """True while Redis is down and the local fallback admits."""
        return self._store.degraded

    async def reserve(
        self, principal: Principal, graph: WorkGraph, *, queue_timeout_s: float, deadline: Deadline | None = None
    ) -> GcraReservation:
        try:
            return await self._reserve(principal, graph, queue_timeout_s, deadline)
        except AdmissionRejected as err:
            if self._instruments is not None:
                self._instruments.add("aghoo_admission_rejections_total", code=err.code.value)
            raise

    async def _reserve(
        self, principal: Principal, graph: WorkGraph, queue_timeout_s: float, deadline: Deadline | None
    ) -> GcraReservation:
        plan = build_plan(self._config.current(), self._keys, principal, graph, deadline, wall_ms(self._clock))
        keys = plan.slot_keys
        full: str | None = None
        if keys and self._queue.blocks(keys):  # FIFO: queued waiters for the same slots go first
            if plan.labeling:  # best effort: labeling never queues, nor jumps ahead of queued waiters
                raise queue_timeout()
            await self._check(plan)  # but rate exhaustion is still a 429 at once
        else:
            first = await self._attempt(plan)
            if isinstance(first, GcraReservation):
                try:
                    await anyio.lowlevel.checkpoint_if_cancelled()  # cancelled during the shielded attempt
                except BaseException:
                    with anyio.CancelScope(shield=True):
                        await self._rollback(first)
                    raise
                return first
            if plan.labeling:
                raise queue_timeout()
            full = first.key
        if queue_timeout_s <= 0:
            raise queue_timeout()

        async def attempt() -> object:
            return await self._attempt(plan)

        try:
            result = await self._queue.wait(plan.tenant_id, keys, queue_timeout_s, attempt, self._rollback, full=full)
        except QueueTimeout:
            raise queue_timeout() from None
        assert isinstance(result, GcraReservation)
        return result

    async def _check(self, plan: Plan) -> None:
        """Raise the 429 of exhausted buckets without debiting anything (a request about to queue behind others)."""
        if plan.buckets:
            now = wall_ms(self._clock)
            verdict, debit = await self._store.reserve(plan.buckets, (), plan.lease_ms, now, check_only=True)
            if verdict.status == "rate":
                raise rejection(plan, verdict, debit)

    async def _attempt(self, plan: Plan) -> GcraReservation | Full:
        """One all-or-nothing try (shielded: a cancelled caller never leaves a half-known debit behind)."""
        with anyio.CancelScope(shield=True):
            verdict, debit = await self._store.reserve(plan.buckets, plan.slots, plan.lease_ms, wall_ms(self._clock))
        if verdict.status == "ok":
            return GcraReservation(self, plan, verdict, debit)
        if verdict.status == "slot":
            return Full(debit.slots[verdict.full_slot].key)
        raise rejection(plan, verdict, debit)

    async def _rollback(self, value: object) -> None:
        """Undo a reservation nobody took (its waiter was cancelled): refund everything, free the slots."""
        if isinstance(value, GcraReservation):
            debit = value._debit
            await self._refund(debit, list(debit.buckets), release=True)

    async def _refund(self, debit: Debit, buckets: Sequence[Bucket], *, release: bool) -> None:
        slots = debit.slots if release else ()
        if buckets or slots:
            await self._store.refund(debit, buckets, slots, wall_ms(self._clock))
        if slots:
            self._queue.released(s.key for s in slots)

    def _overrun(self, kind: CallKind, model: str) -> None:
        if self._instruments is not None:
            self._instruments.add("aghoo_reservation_overrun_total", kind=kind)
        _log.error("reservation overrun: kind=%s model=%s", kind, model)
        if self._env == "test":
            raise AssertionError(f"reservation overrun: {kind} on {model}")

    def _depth(self, depth: int) -> None:
        if self._instruments is not None:
            self._instruments.set("aghoo_queue_depth", depth)
