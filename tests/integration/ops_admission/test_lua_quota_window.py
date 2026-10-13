"""The model-quota window buckets of the admission scripts on the compose Valkey (Wave E2 QL-2): T-GCRA-3, the Redis
script against `LocalStore` (hypothesis: random debits, refunds of earlier debits at their debit time, clock steps;
identical verdicts incl. waits and resets, identical stored slices) and the window property on Redis itself: no 60 s
window holds more than the cap of kept units. Plus the marker undo and the key TTL of a window bucket."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from noema.ops.admission.gcra import QUOTA_SLICES, Bucket, LocalStore, quota_bucket
from noema.ops.admission.stores import RedisStore

from .redis_support import redis_namespace

pytestmark = pytest.mark.integration
T0 = 1_791_000_000_250.0  # wall ms (2026-10), a quarter into a 1 s slice
WINDOW_MS = 60_000.0


@dataclass
class Kept:
    bucket: int
    at_ms: float
    units: int


def bucket(spec: tuple[int, float], i: int, n: int) -> Bucket:
    limit, headroom = spec
    return quota_bucket(f"mdl:t:a{i}:tpm", "tokens", limit, n, alias=f"a{i}", headroom=headroom)


async def stored(client: Any, key: str) -> dict[int, int]:
    return {int(k): int(v) for k, v in (await client.hgetall(key)).items()}


spec = st.tuples(st.sampled_from([1, 7, 10, 600]), st.sampled_from([0.0, 0.2]))
step = st.one_of(
    st.tuples(st.just("debit"), st.lists(st.tuples(st.integers(0, 1), st.integers(0, 12)), min_size=1, max_size=2)),
    st.tuples(st.just("refund"), st.tuples(st.integers(0, 40), st.integers(1, 12))),
    st.tuples(st.just("advance"), st.sampled_from([0.0, 1.0, 249.0, 750.0, 1000.0, 30_000.0, 59_999.0, 61_000.0])),
)


@settings(max_examples=60)
@given(specs=st.tuples(spec, spec), steps=st.lists(step, min_size=1, max_size=30))
async def test_tgcra3_window_script_matches_the_local_store(
    noema_services: Any, noema_run_id: str, specs: tuple[tuple[int, float], tuple[int, float]], steps: list[Any]
) -> None:
    async with redis_namespace(noema_services.redis_url, noema_run_id) as (client, keys):
        redis_store, local, kept, now = RedisStore(client, keys, timeout_s=5.0), LocalStore(), list[Kept](), T0
        for op, arg in steps:
            if op == "advance":
                now += arg
            elif op == "refund":
                k, r = arg
                if k < len(kept) and kept[k].units:
                    entry = kept[k]
                    give = bucket(specs[entry.bucket], entry.bucket, min(r, entry.units))
                    await redis_store.refund([give], [], now, at_ms=entry.at_ms)
                    local.refund([give], [], now, at_ms=entry.at_ms)
                    entry.units -= give.n
            else:
                chosen = dict(arg)  # one debit per bucket
                buckets = [bucket(specs[i], i, n) for i, n in chosen.items()]
                checked = await redis_store.reserve(buckets, [], now, now, check_only=True)
                got = await redis_store.reserve(buckets, [], now, now)
                assert local.reserve(buckets, [], now, now) == got and checked == got
                if got.status == "ok":
                    kept += [Kept(i, now, n) for i, n in chosen.items() if n]
            for i in range(2):  # the same slices on both sides, and the property on what Redis admitted
                assert await stored(client, keys.gcra(f"win:mdl:t:a{i}:tpm")) == local.win.get(f"mdl:t:a{i}:tpm", {})
                mine = [e for e in kept if e.bucket == i]
                for start in mine:
                    window = sum(e.units for e in mine if start.at_ms <= e.at_ms < start.at_ms + WINDOW_MS)
                    assert window <= bucket(specs[i], i, 0).cap


async def test_window_waits_and_resets_follow_the_slices(noema_services: Any, noema_run_id: str) -> None:
    async with redis_namespace(noema_services.redis_url, noema_run_id) as (client, keys):
        store = RedisStore(client, keys, timeout_s=5.0)
        b = quota_bucket("mdl:t:sol:rpm", "requests", 10, 6, alias="sol")
        assert (await store.reserve([b], [], T0, T0)).status == "ok"
        later = T0 + 5_000.0
        assert (await store.reserve([b.with_n(4)], [], later, later)).status == "ok"
        full = await store.reserve([b.with_n(3)], [], later + 1, later + 1)  # 10 counted: the first 6 must leave
        first_slice_ends = (T0 // 1000 + QUOTA_SLICES) * 1000
        assert full.status == "rate" and full.before == (10.0,) and full.after == (13.0,)
        assert full.waits == (first_slice_ends - later - 1,) and full.resets == (first_slice_ends + 5000 - later - 1,)
        assert 62_000 < await client.pttl(keys.gcra("win:" + b.name)) <= 63_000
        await store.refund([b.with_n(4)], [], later + 2, at_ms=later)  # the second debit's slice
        assert await stored(client, keys.gcra("win:" + b.name)) == {int(T0 // 1000): 6}


async def test_a_marker_undo_gives_window_units_back_once(noema_services: Any, noema_run_id: str) -> None:
    async with redis_namespace(noema_services.redis_url, noema_run_id) as (client, keys):
        store = RedisStore(client, keys, timeout_s=5.0)
        b = quota_bucket("mdl:t:sol:tpm", "tokens", 1000, 300, alias="sol")
        marker = keys.gcra("debit:w")
        raw = await store._reserve(
            keys=[keys.gcra("win:" + b.name), marker],
            args=[repr(T0), 1, 0, int(T0 + 9e4), 0, repr(1000.0), 300, 1000, 62],
        )
        assert int(raw[0]) == 0 and await stored(client, keys.gcra("win:" + b.name)) == {int(T0 // 1000): 300}
        for _ in range(2):  # a retried undo gives nothing more back
            await store.refund([b], [], T0 + 10, marker=marker, at_ms=T0)
        assert await stored(client, keys.gcra("win:" + b.name)) == {}


async def test_a_window_bucket_ignores_a_gcra_string_of_the_same_name(noema_services: Any, noema_run_id: str) -> None:
    """A rolling deploy: pods of the previous build keep a GCRA TAT under `gcra:<name>`; no WRONGTYPE either way."""
    async with redis_namespace(noema_services.redis_url, noema_run_id) as (client, keys):
        store = RedisStore(client, keys, timeout_s=5.0)
        b = quota_bucket("mdl:t:sol:rpm", "requests", 10, 2, alias="sol")
        await client.set(keys.gcra(b.name), repr(T0 + 30_000.0), px=60_000)
        assert (await store.reserve([b], [], T0, T0)).status == "ok"
        assert await client.get(keys.gcra(b.name)) == repr(T0 + 30_000.0).encode()
