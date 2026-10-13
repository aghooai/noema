"""GCRA buckets and stream/bulkhead slots (spec-11 §1.2): the shared arithmetic and the in-memory store.

One emission interval `T` and tolerance `τ` per bucket; a debit of n units sets `tat' = max(tat, now) + n·T` and is
admitted iff `tat' - now <= max_busy` (`τ`, or `(1 - LABELING_HEADROOM)·τ` for labeling). A refund of r units sets
`tat = max(now, tat - r·T)`. Times are wall-clock milliseconds (floats) from the caller's `Clock`, so the Redis
script and this module compute bit-identical results from the same inputs (the T-GCRA-1 property); `EPS_MS`
absorbs the float rounding of fractional intervals at exact boundaries.

Model-quota buckets (`mdl:{tenant}:{alias}:rpm|tpm`, `quota_bucket`; Wave E2 QL-2) are sliding windows instead:
no GCRA with `T = 60 000 / L` can keep every 60 s window at <= L calls (it admits up to `τ/T + L` in a window, 1.5x
measured in C1) while still passing a debit of n > 1 units. A window bucket counts its units per 1 s slice
(`QUOTA_SLICE_MS`) and admits a debit iff the units of the last `QUOTA_SLICES` slices plus n stay <= `cap` (the limit,
less the labeling headroom). Units leave 61-62 s after their debit: every 60 s window of admissions, and every window
of calls made up to 1 s after their admission (`QUOTA_MARGIN_MS`), holds at most `limit` units. A refund of r units
takes them off the slice of their debit (nothing if it already left the window).

Slots are sets of reservation members with a lease expiry (ms): expired members are dropped first, then a
reservation is admitted iff `live + count <= limit` (`cap`, less the labeling headroom). The in-memory
`LocalStore` is the Redis-down fail-safe (IMPL-078) and implements exactly the semantics of `lua.RESERVE` /
`lua.REFUND`.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

from noema.core.clock import Clock

EPS_MS: Final = 1e-3
WINDOW_MS: Final = 60_000.0  # admission buckets: limits are per minute and τ is one minute of capacity
LABELING_HEADROOM: Final = 0.2  # labeling debits only while 20% of the model bucket stays free (spec-11 §1.2)
NEVER_RETRY_S: Final = 60  # retry-after of a debit larger than the whole limit
QUOTA_SLICE_MS: Final = 1000.0  # model-quota windows count their units per 1 s slice
QUOTA_MARGIN_MS: Final = 1000.0  # calls are made after their admission (the router decision first): 1 s of slack
QUOTA_SLICES: Final = int((WINDOW_MS + QUOTA_MARGIN_MS) / QUOTA_SLICE_MS) + 1  # 62: the current slice and 61 before
_PRUNE_AT: Final = 4096  # LocalStore drops refilled buckets and empty slot sets past this many entries
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_MS: Final = timedelta(milliseconds=1)

Unit = Literal["requests", "tokens"]
Status = Literal["ok", "rate", "slot"]


def wall_ms(clock: Clock) -> float:
    """The clock's wall time in epoch milliseconds (the GCRA time base shared by every pod)."""
    return (clock.now() - _EPOCH) / _MS


@dataclass(frozen=True, slots=True)
class Bucket:
    """One debit of `n` units on bucket `name` (the `RedisKeys.gcra` suffix, e.g. "key:k1:rpm"). `limit` is the
    header value (units per minute); `threshold_ms` = max_busy + EPS_MS. `alias` is set on per-model buckets.
    `slices` > 0 makes it a sliding-window bucket (model quotas, see the module docstring) admitting up to `cap`."""

    name: str
    unit: Unit
    limit: int
    n: int
    interval_ms: float
    tau_ms: float
    threshold_ms: float
    alias: str | None = None
    slices: int = 0
    headroom: float = 0.0  # window buckets only (GCRA buckets carry it in threshold_ms)

    @property
    def cap(self) -> int:
        """The units a window bucket admits per window: `limit`, or `floor(limit · (1 - headroom))` (labeling)."""
        return self.limit if not self.headroom else math.floor(self.limit * (1.0 - self.headroom) + 1e-9)

    def with_n(self, n: int) -> Bucket:
        """The same bucket with another unit count (refunds)."""
        return Bucket(
            self.name, self.unit, self.limit, n, self.interval_ms, self.tau_ms, self.threshold_ms, self.alias,
            self.slices, self.headroom,
        )  # fmt: skip

    def divided(self, pods: int) -> Bucket:
        """The per-pod share for the Redis-down fallback: `limit // pods` (at least 1) over the same τ, so both
        the rate and the burst shrink by the pod count (IMPL-078); a window bucket's cap shrinks alike."""
        if pods <= 1:
            return self
        share = max(1, self.limit // pods)
        return Bucket(
            self.name, self.unit, share, self.n, self.tau_ms / share, self.tau_ms, self.threshold_ms, self.alias,
            self.slices, self.headroom,
        )  # fmt: skip


def admission_bucket(
    name: str, unit: Unit, limit: int, n: int, *, alias: str | None = None, headroom: float = 0.0
) -> Bucket:
    """A per-minute bucket: `T = 60 000 / limit` ms, `τ = 60 000` ms; `headroom` lowers the admit threshold."""
    return Bucket(name, unit, limit, n, WINDOW_MS / limit, WINDOW_MS, WINDOW_MS * (1.0 - headroom) + EPS_MS, alias)


def quota_bucket(name: str, unit: Unit, limit: int, n: int, *, alias: str, headroom: float = 0.0) -> Bucket:
    """A model-quota bucket: a sliding window of `limit` units per 60 s (QUOTA_SLICES slices of QUOTA_SLICE_MS)."""
    return Bucket(
        name, unit, limit, n, WINDOW_MS / limit, WINDOW_MS, WINDOW_MS * (1.0 - headroom) + EPS_MS, alias,
        QUOTA_SLICES, headroom,
    )  # fmt: skip


def rate_bucket(name: str, rate_per_s: float, burst: int, n: int) -> Bucket:
    """A bare rate bucket (`RateLimiter`): `T = 1000 / rate_per_s` ms, `τ = burst · T`."""
    interval = 1000.0 / rate_per_s
    return Bucket(name, "requests", burst, n, interval, burst * interval, burst * interval + EPS_MS)


@dataclass(frozen=True, slots=True)
class Slot:
    """`members` take places in the slot set `key` (a `RedisKeys.streams` ZSET) of capacity `cap`. With a
    `headroom` (labeling) they are admitted only while that share of the places stays free: up to `limit`."""

    key: str
    cap: int
    members: tuple[str, ...]
    headroom: float = 0.0

    @property
    def limit(self) -> int:
        """The places this reservation may fill: `cap`, or `floor(cap · (1 - headroom))` (0 for a 1-place set)."""
        return self.cap if not self.headroom else math.floor(self.cap * (1.0 - self.headroom) + 1e-9)

    def divided(self, pods: int) -> Slot:
        """The per-pod share of the capacity (at least 1) for the Redis-down fallback; the headroom applies to it."""
        return self if pods <= 1 else Slot(self.key, max(1, self.cap // pods), self.members, self.headroom)


@dataclass(frozen=True, slots=True)
class Verdict:
    """The outcome of one all-or-nothing reserve. `before[i]` = max(tat, now) and `after[i]` = the proposed TAT of
    bucket i (a window bucket: the units counted, and those plus n); nothing was written unless `status == "ok"`.
    `full_slot` indexes the first full slot ("slot"). Window buckets only (0 for GCRA ones): `waits[i]` = ms until
    the debit would fit, `resets[i]` = ms until the counted units have all left the window (before the debit)."""

    status: Status
    now_ms: float
    before: tuple[float, ...]
    after: tuple[float, ...]
    full_slot: int = -1
    waits: tuple[float, ...] = ()
    resets: tuple[float, ...] = ()


def denied(bucket: Bucket, after: float, now_ms: float) -> bool:
    if bucket.slices:
        return after > bucket.cap
    return after - now_ms > bucket.threshold_ms


def retry_after_s(bucket: Bucket, after: float, now_ms: float, wait_ms: float = 0.0) -> int:
    """Whole seconds until the debit would fit: `ceil((tat' - now - τ) / 1000)` (a window bucket: `ceil(wait_ms /
    1000)`), at least 1; 60 when n > limit."""
    if bucket.slices:
        return NEVER_RETRY_S if bucket.n > bucket.limit else max(1, math.ceil((wait_ms - EPS_MS) / 1000.0))
    if bucket.n * bucket.interval_ms > bucket.tau_ms + EPS_MS:
        return NEVER_RETRY_S
    return max(1, math.ceil((after - now_ms - bucket.tau_ms - EPS_MS) / 1000.0))


def remaining(bucket: Bucket, tat: float, now_ms: float) -> int:
    """Units that still fit: `floor((τ - max(0, tat - now)) / T)`; a window bucket (`tat` = the units counted):
    `limit - tat`."""
    if bucket.slices:
        return max(0, bucket.limit - math.ceil(tat))
    busy = max(0.0, tat - now_ms)
    return max(0, math.floor((bucket.tau_ms + EPS_MS - busy) / bucket.interval_ms))


def reset_s(tat: float, now_ms: float) -> int:
    """Whole seconds until the bucket is full again: `ceil(max(0, tat - now) / 1000)`."""
    return max(0, math.ceil((tat - now_ms - EPS_MS) / 1000.0))


def window_reset_s(bucket: Bucket, reset_ms: float, now_ms: float, *, debited: bool) -> int:
    """Whole seconds until a window bucket is empty again: from `reset_ms` (before the debit), or, after a debit of
    n > 0, when the current slice leaves the window."""
    if debited and bucket.n > 0:
        reset_ms = (math.floor(now_ms / QUOTA_SLICE_MS) + bucket.slices) * QUOTA_SLICE_MS - now_ms
    return max(0, math.ceil((reset_ms - EPS_MS) / 1000.0))


class LocalStore:
    """In-memory TATs and slot sets with the exact semantics of the Redis scripts (one pod, no I/O)."""

    __slots__ = ("_next_prune", "slots", "tat", "win")

    def __init__(self) -> None:
        self.tat: dict[str, float] = {}
        self.win: dict[str, dict[int, int]] = {}  # window bucket -> slice index -> units
        self.slots: dict[str, dict[str, float]] = {}  # key -> member -> lease expiry (ms)
        self._next_prune = _PRUNE_AT

    def window(self, b: Bucket, now_ms: float) -> tuple[int, float, float]:
        """(units counted, ms until a debit of `b.n` fits, ms until the window is empty) of window bucket `b`."""
        cur = math.floor(now_ms / QUOTA_SLICE_MS)
        live = sorted((i, c) for i, c in self.win.get(b.name, {}).items() if i > cur - b.slices)
        used = sum(c for _, c in live)
        reset = (live[-1][0] + b.slices) * QUOTA_SLICE_MS - now_ms if live else 0.0
        wait, left = 0.0, used
        if used + b.n > b.cap and b.n <= b.cap:
            for i, c in live:
                left -= c
                if left + b.n <= b.cap:
                    wait = (i + b.slices) * QUOTA_SLICE_MS - now_ms
                    break
        return used, wait, reset

    def reserve(
        self,
        buckets: Sequence[Bucket],
        slots: Sequence[Slot],
        lease_ms: float,
        now_ms: float,
        *,
        check_only: bool = False,
    ) -> Verdict:
        """Debit every bucket and take every slot, or change nothing (see the module docstring). `check_only`
        evaluates the buckets and writes nothing: "ok" means they would pass (`slots` are not looked at)."""
        before: list[float] = []
        after: list[float] = []
        waits: list[float] = []
        resets: list[float] = []
        rejected = False
        for b in buckets:
            if b.slices:
                used, wait, reset = self.window(b, now_ms)
                before.append(float(used))
                after.append(float(used + b.n))
                waits.append(wait)
                resets.append(reset)
                rejected = rejected or used + b.n > b.cap
                continue
            tat = max(self.tat.get(b.name, now_ms), now_ms)
            new = tat + b.n * b.interval_ms
            before.append(tat)
            after.append(new)
            waits.append(0.0)
            resets.append(0.0)
            rejected = rejected or new - now_ms > b.threshold_ms
        shape = (tuple(before), tuple(after))
        if rejected:
            return Verdict("rate", now_ms, *shape, waits=tuple(waits), resets=tuple(resets))
        if check_only:
            return Verdict("ok", now_ms, *shape, waits=tuple(waits), resets=tuple(resets))
        for i, s in enumerate(slots):
            live = self.slots.get(s.key)
            if live:
                for member in [m for m, lease in live.items() if lease <= now_ms]:
                    del live[member]
            if (len(live) if live else 0) + len(s.members) > s.limit:
                return Verdict("slot", now_ms, *shape, i, tuple(waits), tuple(resets))
        cur = math.floor(now_ms / QUOTA_SLICE_MS)
        for b, new in zip(buckets, after, strict=True):
            if b.n > 0 and b.slices:
                kept = {i: c for i, c in self.win.get(b.name, {}).items() if i > cur - b.slices}
                kept[cur] = kept.get(cur, 0) + b.n
                self.win[b.name] = kept
            elif b.n > 0:
                self.tat[b.name] = new
        for s in slots:
            live = self.slots.setdefault(s.key, {})
            for member in s.members:
                live[member] = lease_ms
        if len(self.tat) + len(self.win) + len(self.slots) > self._next_prune:
            self._prune(now_ms)
        return Verdict("ok", now_ms, *shape, waits=tuple(waits), resets=tuple(resets))

    def _prune(self, now_ms: float) -> None:
        self.tat = {name: tat for name, tat in self.tat.items() if tat > now_ms}
        oldest = math.floor(now_ms / QUOTA_SLICE_MS) - QUOTA_SLICES  # every window bucket counts QUOTA_SLICES slices
        self.win = {name: w for name, w in self.win.items() if w and max(w) > oldest}
        self.slots = {key: {m: t for m, t in live.items() if t > now_ms} for key, live in self.slots.items()}
        self.slots = {key: live for key, live in self.slots.items() if live}
        self._next_prune = max(_PRUNE_AT, 2 * (len(self.tat) + len(self.win) + len(self.slots)))  # amortized O(1)

    def refund(
        self, buckets: Sequence[Bucket], slots: Sequence[Slot], now_ms: float, *, at_ms: float | None = None
    ) -> None:
        """`tat = max(now, tat - n·T)` per bucket (n = the refund); a window bucket gives the n units back to the
        slice of its debit at `at_ms` (default `now_ms`) if that slice still counts; then drop the slot members."""
        for b in buckets:
            if b.slices:
                w = self.win.get(b.name)
                at = math.floor((now_ms if at_ms is None else at_ms) / QUOTA_SLICE_MS)
                if w is not None and at in w and b.n > 0:
                    if w[at] > b.n:
                        w[at] -= b.n
                    else:
                        del w[at]
                continue
            tat = self.tat.get(b.name)
            if tat is not None and b.n > 0:
                new = tat - b.n * b.interval_ms
                if new > now_ms:
                    self.tat[b.name] = new
                else:
                    del self.tat[b.name]
        for s in slots:
            live = self.slots.get(s.key)
            if live is not None:
                for member in s.members:
                    live.pop(member, None)

    def peek(self, name: str, now_ms: float) -> float:
        """max(tat, now) of one bucket."""
        return max(self.tat.get(name, now_ms), now_ms)
