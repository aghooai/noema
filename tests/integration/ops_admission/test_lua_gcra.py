"""The admission Lua scripts on the compose Valkey (spec-11 §8): T-GCRA-1, the script against an independent Python
model of the spec-11 §1.2 formulas (hypothesis: random debits, refunds, clock steps over per-minute, labeling-headroom
and RateLimiter buckets; identical allow/deny, retry_after_s and remaining; the check-only mode agrees), plus the
in-process fallback store on the same steps; T-GCRA-2, all-or-nothing debits, check-only writes nothing, and
stream-slot leases, asserted on the Redis state."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from noema.ops.admission.gcra import Bucket, LocalStore, Slot, admission_bucket, rate_bucket, remaining, retry_after_s
from noema.ops.admission.stores import RedisStore

from .redis_support import redis_namespace

pytestmark = pytest.mark.integration
T0 = 1_791_000_000_000.0  # wall ms (2026-10)
LIMITS = (1, 7, 10, 600, 6000)
TOLERANCE_MS = 1e-3  # the boundary tolerance of fractional intervals, stated here independently of the module

Spec = tuple[str, float, float]  # ("adm", limit per minute, headroom) or ("rate", rate per s, burst)


def shape(spec: Spec) -> tuple[float, float, float, float]:
    """spec-11 §1.2 / spec-02 §8: (T, τ, max busy, units that can ever pass) of one bucket."""
    kind, a, b = spec
    if kind == "adm":  # T = 60 000 / L, τ = 60 000; labeling: admitted only while 20% stays free
        return 60_000 / a, 60_000.0, 60_000 * (1 - b), a
    return 1000 / a, b * (1000 / a), b * (1000 / a), b  # RateLimiter: T = 1 / rate, τ = burst · T


def implementation(spec: Spec, name: str, n: int) -> Bucket:
    kind, a, b = spec
    if kind == "adm":
        return admission_bucket(name, "tokens", int(a), n, headroom=b)
    return rate_bucket(name, a, int(b), n)


@dataclass
class Reference:
    """spec-11 §1.2 verbatim: tat' = max(tat, now) + n·T; allowed iff tat' - now <= max busy; refund: max(now,
    tat - r·T); retry_after = ceil((tat' - now - τ) / 1000) >= 1, 60 when n can never pass; remaining =
    floor((τ - max(0, tat - now)) / T)."""

    tat: dict[str, float] = field(default_factory=dict)

    def debit(self, items: list[tuple[str, Spec, int]], now: float) -> tuple[bool, list[float], list[float]]:
        before, after, ok = [], [], True
        for name, spec, n in items:
            interval, _, busy, _ = shape(spec)
            tat = max(self.tat.get(name, now), now)
            before.append(tat)
            after.append(tat + n * interval)
            ok = ok and after[-1] - now <= busy + TOLERANCE_MS
        if ok:
            for (name, _, n), a in zip(items, after, strict=True):
                if n > 0:
                    self.tat[name] = a
        return ok, before, after

    def refund(self, name: str, spec: Spec, r: int, now: float) -> None:
        if name in self.tat and r > 0:
            self.tat[name] = max(now, self.tat[name] - r * shape(spec)[0])

    @staticmethod
    def retry_after(spec: Spec, n: int, after: float, now: float) -> int:
        _, tau, _, units = shape(spec)
        return 60 if n > units else max(1, math.ceil((after - now - tau - TOLERANCE_MS) / 1000))

    @staticmethod
    def remaining(spec: Spec, tat: float, now: float) -> int:
        interval, tau, _, _ = shape(spec)
        return max(0, math.floor((tau + TOLERANCE_MS - max(0.0, tat - now)) / interval))


spec = st.one_of(
    st.tuples(st.just("adm"), st.sampled_from(LIMITS), st.sampled_from([0.0, 0.2])),
    st.tuples(st.just("rate"), st.sampled_from([0.5, 3.0, 10.0]), st.sampled_from([1, 5, 50])),
)
step = st.one_of(
    st.tuples(st.just("debit"), st.lists(st.tuples(st.integers(0, 2), st.integers(0, 12)), min_size=1, max_size=3)),
    st.tuples(st.just("refund"), st.tuples(st.integers(0, 2), st.integers(0, 12))),
    st.tuples(st.just("advance"), st.sampled_from([0.0, 0.5, 250.0, 1000.0, 5999.0, 6000.0, 30_000.0, 61_000.0])),
)


@given(specs=st.tuples(spec, spec, spec), steps=st.lists(step, min_size=1, max_size=25))
async def test_tgcra1_script_matches_the_reference_model(
    noema_services: Any, noema_run_id: str, specs: tuple[Spec, Spec, Spec], steps: list[tuple[Any, ...]]
) -> None:
    async with redis_namespace(noema_services.redis_url, noema_run_id) as (client, keys):
        redis_store, local, ref = RedisStore(client, keys, timeout_s=5.0), LocalStore(), Reference()
        names = [f"b{i}" for i in range(3)]
        now = T0
        for op, arg in steps:
            if op == "advance":
                now += arg
            elif op == "refund":
                i, r = arg
                bucket = implementation(specs[i], names[i], r)
                ref.refund(names[i], specs[i], r, now)
                await redis_store.refund([bucket], [], now)
                local.refund([bucket], [], now)
            else:
                chosen = list(dict(arg).items())  # one debit per bucket
                buckets = [implementation(specs[i], names[i], n) for i, n in chosen]
                ok, before, after = ref.debit([(names[i], specs[i], n) for i, n in chosen], now)
                assert (await redis_store.reserve(buckets, [], now, now, check_only=True)).status == (
                    "ok" if ok else "rate"
                )
                got = await redis_store.reserve(buckets, [], now, now)
                assert (got.status == "ok", list(got.before), list(got.after)) == (ok, before, after)
                assert local.reserve(buckets, [], now, now) == got
                for (i, n), b, a, t in zip(chosen, buckets, after, before, strict=True):
                    assert remaining(b, t, now) == Reference.remaining(specs[i], t, now)
                    if a - now > shape(specs[i])[2] + TOLERANCE_MS:
                        assert retry_after_s(b, a, now) == Reference.retry_after(specs[i], n, a, now)
        for name in names:  # the stored state means the same: max(tat, now)
            raw = await client.get(keys.gcra(name))
            stored = now if raw is None else max(float(raw), now)
            assert stored == max(ref.tat.get(name, now), now) == local.peek(name, now)


async def test_tgcra2_one_failing_bucket_or_slot_writes_nothing(noema_services: Any, noema_run_id: str) -> None:
    async with redis_namespace(noema_services.redis_url, noema_run_id) as (client, keys):
        store = RedisStore(client, keys, timeout_s=5.0)
        ok_a = admission_bucket("a:rpm", "requests", 10, 3)
        ok_b = admission_bucket("b:tpm", "tokens", 1000, 10)
        too_big = admission_bucket("c:rpm", "requests", 5, 6)
        slot_key = keys.streams("t1", "p1")
        checked = await store.reserve([ok_a, ok_b], [Slot(slot_key, 5, ("r0",))], T0 + 60_000, T0, check_only=True)
        assert checked.status == "ok" and checked.after == (T0 + 18_000, T0 + 600)
        assert await client.exists(keys.gcra("a:rpm"), keys.gcra("b:tpm"), slot_key) == 0  # check-only: no write
        verdict = await store.reserve([ok_a, ok_b, too_big], [Slot(slot_key, 5, ("r1",))], T0 + 60_000, T0)
        assert verdict.status == "rate"
        assert [await client.exists(keys.gcra(n)) for n in ("a:rpm", "b:tpm", "c:rpm")] == [0, 0, 0]
        assert await client.exists(slot_key) == 0
        full = Slot(keys.streams("t1", "full"), 1, ("r2",))
        assert (await store.reserve([], [full], T0 + 60_000, T0)).status == "ok"
        verdict = await store.reserve([ok_a], [Slot(slot_key, 5, ("r3",)), Slot(full.key, 1, ("r3",))], T0 + 1, T0)
        assert (verdict.status, verdict.full_slot) == ("slot", 1)
        assert await client.exists(keys.gcra("a:rpm")) == 0 and await client.zcard(slot_key) == 0
        verdict = await store.reserve([ok_a, ok_b], [Slot(slot_key, 5, ("r4",))], T0 + 60_000, T0)
        assert verdict.status == "ok"
        stored = await client.get(keys.gcra("a:rpm"))
        assert stored is not None and float(stored) == T0 + 18_000
        assert 18_000 < await client.pttl(keys.gcra("a:rpm")) <= 19_000  # ceil(tat - now) + 1 s
        assert await client.zscore(slot_key, "r4") == T0 + 60_000
        assert 60_000 < await client.pttl(slot_key) <= 61_000


async def test_tgcra2_stream_slots_expire_with_the_lease_and_refunds_free_them(
    noema_services: Any, noema_run_id: str
) -> None:
    async with redis_namespace(noema_services.redis_url, noema_run_id) as (client, keys):
        store = RedisStore(client, keys, timeout_s=5.0)
        key = keys.streams("t1", "p1")
        assert (await store.reserve([], [Slot(key, 2, ("dead",))], T0 + 1000, T0)).status == "ok"
        assert (await store.reserve([], [Slot(key, 2, ("live",))], T0 + 90_000, T0)).status == "ok"
        assert (await store.reserve([], [Slot(key, 2, ("late",))], T0 + 90_000, T0 + 999)).status == "slot"
        assert (await store.reserve([], [Slot(key, 2, ("late",))], T0 + 90_000, T0 + 1000)).status == "ok"
        assert sorted(await client.zrange(key, 0, -1)) == [b"late", b"live"]  # the dead pod's slot is gone
        many = Slot(keys.streams("t1", "p2"), 3, ("m#0", "m#1"))  # a 2-stream reservation takes 2 places
        assert (await store.reserve([], [many], T0 + 5000, T0)).status == "ok"
        assert (await store.reserve([], [Slot(many.key, 3, ("x#0", "x#1"))], T0 + 5000, T0)).status == "slot"
        bucket = admission_bucket("a:rpm", "requests", 10, 4)
        await store.reserve([bucket], [], T0, T0)
        await store.refund([bucket.with_n(4)], [Slot(key, 2, ("live",)), many], T0)
        assert await client.exists(keys.gcra("a:rpm")) == 0  # fully refunded: back to "now"
        assert await client.zrange(key, 0, -1) == [b"late"] and await client.zcard(many.key) == 0


async def test_an_undo_with_the_marker_is_exactly_once_and_voids_a_late_reserve(
    noema_services: Any, noema_run_id: str
) -> None:
    """Wave E edge fix 1 (lua.REFUND with a marker): the undo refunds only what its reserve wrote, once, and a
    reserve that arrives after its undo writes nothing."""
    async with redis_namespace(noema_services.redis_url, noema_run_id) as (client, keys):
        store = RedisStore(client, keys, timeout_s=1.0)
        bucket, slot = admission_bucket("key:k:rpm", "requests", 10, 2), Slot(keys.streams("t", "p"), 5, ("m",))
        written, marker = keys.gcra("debit:a"), keys.gcra("debit:b")
        reserve = store._reserve
        await reserve(
            keys=[keys.gcra("key:k:rpm"), slot.key, written],
            args=[repr(T0), 1, 1, int(T0 + 9e4), 0, repr(6000.0), 2, repr(60000.0), 0, 5, 1, "m"],
        )
        assert await client.get(written) == b"1"
        for _ in range(2):  # a retried undo refunds nothing more
            await store.refund([bucket], [slot], T0, marker=written)
        assert await client.get(keys.gcra("key:k:rpm")) is None and await client.zcard(slot.key) == 0
        await store.refund([bucket], [slot], T0, marker=marker)  # the undo of a reserve that has not run yet
        raw = await reserve(
            keys=[keys.gcra("key:k:rpm"), slot.key, marker],
            args=[repr(T0), 1, 1, int(T0 + 9e4), 0, repr(6000.0), 2, repr(60000.0), 0, 5, 1, "m"],
        )
        assert int(raw[0]) == 3  # refused: nothing written
        assert await client.get(keys.gcra("key:k:rpm")) is None and await client.zcard(slot.key) == 0
