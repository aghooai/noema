"""Where the buckets live: the Redis scripts, and the Redis-down fail-safe (spec-11 §1.2, IMPL-078).

`FailoverStore` uses Redis while it answers. A script error, a connection error or a call slower than
`REDIS_TIMEOUT_S` switches it to a per-pod `LocalStore` at `limit // expected_pods` of every bucket and slot set;
Redis is probed again every `PROBE_INTERVAL_S` (by the next reserve), and on the first success of a call that
started while degraded (a probe; a call already in flight when another one failed does not count) new reservations
use Redis again and the local state is set aside, never merged. Hysteresis: if Redis fails again within
`LOCAL_KEEP_S` the set-aside state is reused, so a flapping Redis does not hand each pod a fresh local burst per
flap. A debit remembers its store, so its refunds go back where it was debited.

A failed writing reserve may still have run on Redis (a reply later than the bound, a connection lost after the
send): `RedisStore` then undoes it at once with one bounded `REFUND` carrying the reserve's marker (`lua.py`), which
refunds the buckets only if that reserve wrote and voids it if it arrives later; slot members whose undo failed are
kept with the failed slot releases (below). So a timeout never leaves a Redis debit that nothing refunds.

Metrics (with `instruments`): `aghoo_admission_degraded` (1 while the fallback admits, else 0),
`aghoo_admission_refund_failures_total` (Redis refunds that failed or were cancelled) and
`aghoo_admission_pending_releases` (the kept slot releases); both gauges start at 0.
"""

from __future__ import annotations

import logging
import math
import secrets
from collections import deque
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import anyio
from redis.exceptions import RedisError

from noema.core.clock import Clock
from noema.core.protocols.telemetry import Instruments
from noema.core.redis_keys import RedisKeys
from noema.ops.admission import lua
from noema.ops.admission.gcra import QUOTA_SLICE_MS, Bucket, LocalStore, Slot, Status, Verdict

if TYPE_CHECKING:
    import redis.asyncio

REDIS_TIMEOUT_S: Final = 0.05  # a slower script call counts as "Redis down" (spec-11 §1.2)
PROBE_INTERVAL_S: Final = 1.0
LOCAL_KEEP_S: Final = 60.0  # the set-aside local state is reused this long after recovery (one admission τ)
MAX_PENDING_SLOTS: Final = 4096  # failed slot releases kept for a retry (oldest dropped: their leases expire)
RETRY_BATCH: Final = 256  # kept slot releases carried by one Redis refund
_STATUS: Final[tuple[Status, ...]] = ("ok", "rate", "slot")
_log: Final = logging.getLogger("noema.ops.admission")


class StoreError(Exception):
    """The shared store failed: a script or connection error, or the call timed out. `unreleased`: slots of a
    failed reserve that may hold its members on Redis (their undo failed too)."""

    def __init__(self, reason: str, unreleased: tuple[Slot, ...] = ()) -> None:
        super().__init__(reason)
        self.unreleased = unreleased


def _num(x: float) -> str:
    return repr(float(x))


def _bucket_args(b: Bucket) -> list[str | int]:
    """RESERVE's per-bucket arguments: GCRA (T, n, max busy, 0) or window (slice width, n, cap, slices)."""
    if b.slices:
        return [_num(QUOTA_SLICE_MS), b.n, b.cap, b.slices]
    return [_num(b.interval_ms), b.n, _num(b.threshold_ms), 0]


class RedisStore:
    """Runs `lua.RESERVE` / `lua.REFUND` on one Redis/Valkey; bucket keys are `RedisKeys.gcra(bucket.name)` (a
    window bucket: `RedisKeys.gcra("win:" + bucket.name)`, a hash, so it never meets a GCRA string of the same name
    during a rolling deploy), slot keys are used as given. Every call is bounded by `timeout_s` of real time (an I/O
    bound, not request logic)."""

    def __init__(self, redis: redis.asyncio.Redis, keys: RedisKeys, *, timeout_s: float = REDIS_TIMEOUT_S) -> None:
        self._redis = redis
        self._keys = keys
        self._timeout_s = timeout_s
        self._reserve = redis.register_script(lua.RESERVE)
        self._refund = redis.register_script(lua.REFUND)

    async def reserve(
        self,
        buckets: Sequence[Bucket],
        slots: Sequence[Slot],
        lease_ms: float,
        now_ms: float,
        *,
        check_only: bool = False,
    ) -> Verdict:
        """`check_only`: evaluate the buckets, write nothing, ignore `slots` (see `LocalStore.reserve`)."""
        if check_only:
            slots = ()
        marker = None if check_only else self._keys.gcra("debit:" + secrets.token_hex(8))
        keys = [self._bucket_key(b) for b in buckets] + [s.key for s in slots] + ([marker] if marker else [])
        args: list[str | int] = [_num(now_ms), len(buckets), len(slots), int(lease_ms), int(check_only)]
        for b in buckets:
            args += _bucket_args(b)
        for s in slots:
            args += [s.limit, len(s.members), *s.members]
        try:
            raw: list[bytes | int] = await self._bounded(self._reserve(keys=keys, args=args))
        except StoreError as exc:
            if marker is None:
                raise
            undone = await self._undo(buckets, slots, marker, now_ms)
            raise StoreError(str(exc), () if undone else tuple(slots)) from exc
        if int(raw[0]) == 3:
            raise StoreError("marker exists")  # a fresh random marker: unreachable unless it collided
        values = [float(v) for v in raw[2:]]
        status = _STATUS[int(raw[0])]
        full = int(raw[1]) if status == "slot" else -1
        before, after, waits, resets = (tuple(values[k::4]) for k in range(4))  # 4 values per bucket
        return Verdict(status, now_ms, before, after, full, waits, resets)

    async def refund(
        self,
        buckets: Sequence[Bucket],
        slots: Sequence[Slot],
        now_ms: float,
        *,
        marker: str | None = None,
        at_ms: float | None = None,
    ) -> None:
        """`marker`: undo the reserve of that marker (its buckets only if it wrote; see `lua.REFUND`). `at_ms`: when
        the refunded units were debited (window buckets give them back to that slice; default `now_ms`)."""
        keys = [self._bucket_key(b) for b in buckets] + [s.key for s in slots] + ([marker] if marker else [])
        args: list[str | int] = [_num(now_ms), len(buckets), len(slots)]
        at = _num(now_ms if at_ms is None else at_ms)
        for b in buckets:
            args += [_num(QUOTA_SLICE_MS), b.n, b.slices, at] if b.slices else [_num(b.interval_ms), b.n, 0, at]
        for s in slots:
            args += [len(s.members), *s.members]
        await self._bounded(self._refund(keys=keys, args=args))

    def _bucket_key(self, b: Bucket) -> str:
        return self._keys.gcra("win:" + b.name if b.slices else b.name)

    async def _undo(self, buckets: Sequence[Bucket], slots: Sequence[Slot], marker: str, now_ms: float) -> bool:
        """Take back a reserve that failed but may have run (False: the undo failed too)."""
        try:
            await self.refund(buckets, slots, now_ms, marker=marker)
        except StoreError as exc:
            _log.warning("admission: undoing a failed Redis reserve failed (%s); its slot releases are kept", exc)
            return False
        return True

    async def peek(self, name: str, now_ms: float) -> float:
        """max(tat, now) of one bucket."""
        raw = await self._bounded(self._redis.get(self._keys.gcra(name)))
        return now_ms if raw is None else max(float(raw), now_ms)

    async def _bounded[T](self, call: Awaitable[T]) -> T:
        try:
            with anyio.fail_after(self._timeout_s):
                return await call
        except (RedisError, OSError, TimeoutError) as exc:
            raise StoreError(type(exc).__name__) from exc


@dataclass(frozen=True, slots=True)
class Debit:
    """What one reserve debited, where and when (`at_ms`): refunds and slot releases go back to the same store (and
    a window bucket's units to the slice of `at_ms`)."""

    store: RedisStore | LocalStore
    buckets: tuple[Bucket, ...]
    slots: tuple[Slot, ...]
    at_ms: float


class FailoverStore:
    """Redis while it answers, else the per-pod local share (see the module docstring). `redis=None` runs on the
    local store only (single-process dev and tests); then nothing is divided."""

    def __init__(
        self,
        redis: RedisStore | None,
        clock: Clock,
        *,
        expected_pods: int = 1,
        probe_interval_s: float = PROBE_INTERVAL_S,
        instruments: Instruments | None = None,
    ) -> None:
        if expected_pods < 1:
            raise ValueError("expected_pods must be >= 1")
        self._redis = redis
        self._clock = clock
        self._instruments = instruments
        self._pods = expected_pods if redis is not None else 1
        self._probe_interval_s = probe_interval_s
        self._local = LocalStore()
        self._down_until: float | None = None
        self._keep_local_until = -math.inf
        self._pending: deque[Slot] = deque(maxlen=MAX_PENDING_SLOTS)
        self._gauge("aghoo_admission_degraded", 0)
        self._gauge("aghoo_admission_pending_releases", 0)

    @property
    def degraded(self) -> bool:
        """True while Redis is considered down and reservations go to the local fallback."""
        return self._down_until is not None

    @property
    def pending_releases(self) -> int:
        """Slot releases of Redis debits that failed and wait to be carried by the next Redis refund."""
        return len(self._pending)

    def _use_redis(self) -> RedisStore | None:
        if self._redis is not None and (self._down_until is None or self._clock.monotonic() >= self._down_until):
            return self._redis
        return None

    async def reserve(
        self,
        buckets: Sequence[Bucket],
        slots: Sequence[Slot],
        lease_ms: float,
        now_ms: float,
        *,
        check_only: bool = False,
    ) -> tuple[Verdict, Debit]:
        """All-or-nothing reserve on the current store (never raises for a store failure). `check_only` evaluates
        the buckets and writes nothing (`LocalStore.reserve`)."""
        if (redis := self._use_redis()) is not None:
            probe = self._down_until is not None
            try:
                verdict = await redis.reserve(buckets, slots, lease_ms, now_ms, check_only=check_only)
            except StoreError as exc:
                self._mark_down(exc)
                self._keep(exc.unreleased)
            else:
                if probe and self._down_until is not None:
                    self._recovered()
                return verdict, Debit(redis, tuple(buckets), tuple(slots), now_ms)
        local_buckets = tuple(b.divided(self._pods) for b in buckets)
        local_slots = tuple(s.divided(self._pods) for s in slots)
        verdict = self._local.reserve(local_buckets, local_slots, lease_ms, now_ms, check_only=check_only)
        return verdict, Debit(self._local, local_buckets, local_slots, now_ms)

    async def refund(self, debit: Debit, buckets: Sequence[Bucket], slots: Sequence[Slot], now_ms: float) -> None:
        """Refund `buckets` (n = the units to give back) and free `slots` on the store that debited them. A Redis
        debit goes back to Redis even while reserves use the fallback (one bounded call, off the request path); a
        failing refund never marks Redis down. If it fails, its bucket refunds are lost (a call that timed out may
        have run, so a retry could refund twice; the buckets refill on their own) and its slot releases are kept
        (bounded, oldest dropped) and carried by the next Redis refund: ZREM is idempotent."""
        if isinstance(debit.store, LocalStore):
            debit.store.refund(buckets, slots, now_ms, at_ms=debit.at_ms)  # a set-aside generation: garbage later
            return
        retry = [self._pending.popleft() for _ in range(min(RETRY_BATCH, len(self._pending)))]
        done = False
        try:
            await debit.store.refund(buckets, (*slots, *retry), now_ms, at_ms=debit.at_ms)
            done = True
        except StoreError as exc:
            if not retry:
                _log.warning("admission: a Redis refund failed (%s); its slot releases are kept for a retry", exc)
        finally:
            if not done:  # also when cancelled (the release shield ran out): keep the releases
                self._pending.extendleft(reversed(retry))
                self._pending.extend(slots)
                if self._instruments is not None:
                    self._instruments.add("aghoo_admission_refund_failures_total")
            if retry or not done:  # the kept list changed
                self._gauge("aghoo_admission_pending_releases", len(self._pending))

    async def peek(self, bucket: Bucket, now_ms: float) -> tuple[float, Bucket]:
        """max(tat, now) of one bucket and the bucket as the answering store counts it."""
        if (redis := self._use_redis()) is not None:
            probe = self._down_until is not None
            try:
                tat = await redis.peek(bucket.name, now_ms)
            except StoreError as exc:
                self._mark_down(exc)
            else:
                if probe and self._down_until is not None:
                    self._recovered()
                return tat, bucket
        return self._local.peek(bucket.name, now_ms), bucket.divided(self._pods)

    def _keep(self, slots: Sequence[Slot]) -> None:
        """Keep the slot releases of a failed undo for the next Redis refund (counted as a failed refund)."""
        if slots:
            self._pending.extend(slots)
            self._gauge("aghoo_admission_pending_releases", len(self._pending))
            if self._instruments is not None:
                self._instruments.add("aghoo_admission_refund_failures_total")

    def _mark_down(self, exc: StoreError) -> None:
        now = self._clock.monotonic()
        if self._down_until is None:
            if now >= self._keep_local_until:  # else a flapping Redis: reuse the recent share, no fresh burst
                self._local = LocalStore()
            _log.warning("admission: Redis unavailable (%s); local buckets at 1/%d of each limit", exc, self._pods)
            self._gauge("aghoo_admission_degraded", 1)
        self._down_until = now + self._probe_interval_s

    def _recovered(self) -> None:
        _log.info("admission: Redis is back; local admission state set aside (not merged)")
        self._down_until = None
        self._keep_local_until = self._clock.monotonic() + LOCAL_KEEP_S
        self._gauge("aghoo_admission_degraded", 0)

    def _gauge(self, name: str, value: int) -> None:
        if self._instruments is not None:
            self._instruments.set(name, value)
