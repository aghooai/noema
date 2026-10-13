"""`FailoverStore` (spec-11 §1.2, IMPL-078) on a scripted primary: refunds of Redis debits go back to Redis while
reserves use the fallback, failed slot releases are retried with the next Redis refund (bounded), and a flapping
Redis reuses the recent local share instead of handing out a fresh burst per flap."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

import anyio
import pytest
from tests.fakes.clock import FakeClock
from tests.fakes.telemetry import FakeTelemetry

from noema.ops.admission import stores
from noema.ops.admission.gcra import Bucket, LocalStore, Slot, Verdict, admission_bucket, wall_ms
from noema.ops.admission.stores import LOCAL_KEEP_S, FailoverStore, StoreError

pytestmark = pytest.mark.unit
LEASE_MS = 300_000.0


class ScriptedRedis:
    """A `RedisStore` stand-in on in-memory state: the next `fail_reserves` reserves fail, refunds fail while
    `refunds_down`."""

    def __init__(self) -> None:
        self.state = LocalStore()
        self.fail_reserves = 0
        self.refunds_down = False

    async def reserve(
        self, buckets: Sequence[Bucket], slots: Sequence[Slot], lease_ms: float, now_ms: float, **kw: Any
    ) -> Verdict:
        if self.fail_reserves:
            self.fail_reserves -= 1
            raise StoreError("TimeoutError")
        return self.state.reserve(buckets, slots, lease_ms, now_ms, **kw)

    async def refund(
        self, buckets: Sequence[Bucket], slots: Sequence[Slot], now_ms: float, *, at_ms: float | None = None
    ) -> None:
        if self.refunds_down:
            raise StoreError("ConnectionError")
        self.state.refund(buckets, slots, now_ms, at_ms=at_ms)


def failover(
    clock: FakeClock, pods: int = 4, telemetry: FakeTelemetry | None = None
) -> tuple[ScriptedRedis, FailoverStore]:
    primary = ScriptedRedis()
    instruments = telemetry.instruments if telemetry is not None else None
    return primary, FailoverStore(primary, clock, expected_pods=pods, instruments=instruments)  # type: ignore[arg-type]


def degraded(store: FailoverStore) -> bool:
    return store.degraded  # a call, so mypy does not narrow the property across the reserves


def gauge(telemetry: FakeTelemetry, name: str) -> list[float]:
    return [m.value for m in telemetry.metrics if m.name == name]


def rpm(limit: int = 10, n: int = 1) -> Bucket:
    return admission_bucket("key:k:rpm", "requests", limit, n)


async def test_refunds_of_redis_debits_go_to_redis_while_reserves_are_degraded() -> None:
    clock = FakeClock()
    primary, store = failover(clock)
    now = wall_ms(clock)
    verdict, debit = await store.reserve([rpm(n=5)], [Slot("s", 2, ("m1",))], now + LEASE_MS, now)
    assert verdict.status == "ok" and debit.store is cast(object, primary)
    primary.fail_reserves = 1
    await store.reserve([rpm()], [], now, now)  # one unrelated reserve over the bound: degraded for 1 s
    assert store.degraded
    await store.refund(debit, [rpm(n=4)], debit.slots, now)
    assert primary.state.slots["s"] == {} and primary.state.peek("key:k:rpm", now) == now + 6000
    assert degraded(store) and store.pending_releases == 0  # a refund is not a probe


async def test_failed_slot_releases_are_carried_by_the_next_redis_refund() -> None:
    clock = FakeClock()
    primary, store = failover(clock)
    now = wall_ms(clock)
    _, first = await store.reserve([rpm()], [Slot("s", 2, ("m1",))], now + LEASE_MS, now)
    _, second = await store.reserve([rpm()], [Slot("s", 2, ("m2",))], now + LEASE_MS, now)
    primary.refunds_down = True
    await store.refund(first, [rpm()], first.slots, now)
    assert store.pending_releases == 1 and not store.degraded  # a failing refund never marks Redis down
    assert set(primary.state.slots["s"]) == {"m1", "m2"}
    primary.refunds_down = False
    await store.refund(second, [], second.slots, now)
    assert primary.state.slots["s"] == {} and store.pending_releases == 0
    assert primary.state.peek("key:k:rpm", now) == now + 2 * 6000  # the failed bucket refund is lost (fail-safe)


async def test_a_cancelled_redis_refund_keeps_its_slot_releases() -> None:
    clock = FakeClock()
    telemetry = FakeTelemetry()
    primary, store = failover(clock, telemetry=telemetry)
    now = wall_ms(clock)
    _, debit = await store.reserve([], [Slot("s", 2, ("m1",))], now + LEASE_MS, now)
    started, never = anyio.Event(), anyio.Event()

    async def hangs(buckets: Sequence[Bucket], slots: Sequence[Slot], now_ms: float, **_: Any) -> None:
        started.set()
        await never.wait()

    primary.refund = hangs  # type: ignore[method-assign]
    async with anyio.create_task_group() as tg:
        tg.start_soon(store.refund, debit, [], debit.slots, now)
        await started.wait()
        tg.cancel_scope.cancel()  # e.g. the release shield ran out
    assert store.pending_releases == 1
    assert telemetry.counter("aghoo_admission_refund_failures_total") == 1  # a cancelled refund failed too
    assert gauge(telemetry, "aghoo_admission_pending_releases") == [0, 1]


async def test_kept_slot_releases_are_bounded_and_retried_in_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stores, "MAX_PENDING_SLOTS", 3)
    monkeypatch.setattr(stores, "RETRY_BATCH", 2)
    clock = FakeClock()
    primary, store = failover(clock)
    now = wall_ms(clock)
    debits = [(await store.reserve([], [Slot("s", 9, (f"m{i}",))], now + LEASE_MS, now))[1] for i in range(6)]
    primary.refunds_down = True
    for debit in debits[:5]:
        await store.refund(debit, [], debit.slots, now)
    assert store.pending_releases == 3  # m0 and m1 dropped: their leases expire on their own
    primary.refunds_down = False
    await store.refund(debits[5], [], debits[5].slots, now)  # carries m2, m3
    assert sorted(primary.state.slots["s"]) == ["m0", "m1", "m4"] and store.pending_releases == 1
    await store.refund(debits[5], [], (), now)  # carries m4
    assert sorted(primary.state.slots["s"]) == ["m0", "m1"] and store.pending_releases == 0


async def test_a_flapping_redis_reuses_the_recent_local_share() -> None:
    clock = FakeClock()
    primary, store = failover(clock)  # 40 per minute, 4 pods: 10 per minute on the local share
    admitted = 0
    for _ in range(10):  # 10 s: Redis answers once (recovered), then a call fails and 20 go to the local share
        now = wall_ms(clock)
        await store.reserve([], [], now, now)
        assert not degraded(store)
        primary.fail_reserves = 1
        for _ in range(20):
            admitted += (await store.reserve([rpm(40)], [], now, now))[0].status == "ok"
        assert store.degraded
        await clock.advance(1.0)
    assert admitted == 11  # one share: a burst of 10 plus one call per 6 s (a fresh share per flap: 100)


async def test_the_set_aside_share_is_reused_only_within_the_keep_window() -> None:
    clock = FakeClock()
    primary, store = failover(clock)  # a 4-place slot set: 1 place per pod while degraded

    async def degraded_reserve(member: str) -> str:
        primary.fail_reserves = 1
        now = wall_ms(clock)
        verdict, _ = await store.reserve([], [Slot("s", 4, (member,))], now + 10 * LEASE_MS, now)
        assert store.degraded
        return verdict.status

    async def recover() -> None:
        await clock.advance(1.0)  # the next probe
        now = wall_ms(clock)
        await store.reserve([], [], now, now)
        assert not store.degraded

    assert await degraded_reserve("m1") == "ok"  # a long stream admitted on the local share
    await recover()
    assert await degraded_reserve("m2") == "slot"  # Redis fails again within the window: m1 still counts
    await recover()
    await clock.advance(LOCAL_KEEP_S)
    assert await degraded_reserve("m3") == "ok"  # a later outage starts from a fresh share (dropped, not merged)


async def test_degraded_refund_failure_and_pending_release_metrics() -> None:
    clock = FakeClock()
    telemetry = FakeTelemetry()
    primary, store = failover(clock, telemetry=telemetry)
    assert gauge(telemetry, "aghoo_admission_degraded") == [0]  # both gauges start at 0
    assert gauge(telemetry, "aghoo_admission_pending_releases") == [0]
    now = wall_ms(clock)
    _, debit = await store.reserve([rpm()], [Slot("s", 2, ("m1",))], now + LEASE_MS, now)
    primary.fail_reserves = 1
    for _ in range(3):  # set once on the way down, not per degraded reserve
        await store.reserve([rpm()], [], now, now)
    await clock.advance(1.0)  # the next probe succeeds
    await store.reserve([], [], wall_ms(clock), wall_ms(clock))
    assert gauge(telemetry, "aghoo_admission_degraded") == [0, 1, 0]
    primary.refunds_down = True
    await store.refund(debit, [rpm()], debit.slots, now)  # fails: m1 kept
    await store.refund(debit, [rpm()], (), now)  # fails again: m1 still kept
    primary.refunds_down = False
    await store.refund(debit, [rpm()], (), now)  # carries m1
    await store.refund(debit, [rpm()], (), now)  # nothing kept: the gauge is not touched
    assert telemetry.counter("aghoo_admission_refund_failures_total") == 2
    assert gauge(telemetry, "aghoo_admission_pending_releases") == [0, 1, 1, 0]


async def test_a_success_that_started_before_the_cut_does_not_end_the_degraded_state() -> None:
    """Wave E edge fix 1: only a call that started while degraded (a probe) may end the degraded state; a call
    in flight when another one failed must not flip it back within milliseconds."""
    clock = FakeClock()
    primary, store = failover(clock)
    now = wall_ms(clock)
    started, reply = anyio.Event(), anyio.Event()
    answer = primary.reserve

    async def slow(
        buckets: Sequence[Bucket], slots: Sequence[Slot], lease_ms: float, now_ms: float, **kw: Any
    ) -> Verdict:
        started.set()
        await reply.wait()
        return await answer(buckets, slots, lease_ms, now_ms, **kw)

    async with anyio.create_task_group() as tg:
        primary.reserve = slow  # type: ignore[method-assign]
        tg.start_soon(store.reserve, [rpm()], [], now, now)
        await started.wait()
        primary.reserve = answer  # type: ignore[method-assign]
        primary.fail_reserves = 1
        await store.reserve([rpm()], [], now, now)  # times out: degraded
        assert store.degraded
        reply.set()  # the earlier call succeeds afterwards
    assert degraded(store)
    await clock.advance(1.0)
    await store.reserve([], [], wall_ms(clock), wall_ms(clock))  # the probe
    assert not degraded(store)
