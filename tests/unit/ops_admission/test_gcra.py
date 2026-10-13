"""GCRA arithmetic and `LocalStore` (spec-11 §1.2): debit/refund/remaining/reset/retry-after formulas,
all-or-nothing debits, slot leases, and the per-pod division of the Redis-down fallback."""

from __future__ import annotations

import pytest
from tests.fakes.clock import DEFAULT_WALL, FakeClock

from noema.ops.admission.gcra import (
    LABELING_HEADROOM,
    QUOTA_SLICES,
    LocalStore,
    Slot,
    admission_bucket,
    denied,
    quota_bucket,
    rate_bucket,
    remaining,
    reset_s,
    retry_after_s,
    wall_ms,
)

pytestmark = pytest.mark.unit
NOW = 1_000_000.0


def test_wall_ms_follows_the_clock() -> None:
    clock = FakeClock()
    assert wall_ms(clock) == DEFAULT_WALL.timestamp() * 1000


async def test_wall_ms_advances_with_virtual_time() -> None:
    clock = FakeClock()
    start = wall_ms(clock)
    await clock.advance(1.5)
    assert wall_ms(clock) - start == 1500.0


def test_formulas_of_a_per_minute_bucket() -> None:
    b = admission_bucket("key:k:rpm", "requests", 10, 4)  # T = 6000 ms, tau = 60 000 ms
    assert (b.interval_ms, b.tau_ms) == (6000.0, 60_000.0)
    after = NOW + 4 * 6000
    assert not denied(b, after, NOW)
    assert remaining(b, after, NOW) == 6 and reset_s(after, NOW) == 24
    assert remaining(b, NOW, NOW) == 10 and reset_s(NOW, NOW) == 0
    over = NOW + 66_000  # 11 units: 6 s over tau
    assert denied(b, over, NOW) and retry_after_s(b, over, NOW) == 6
    assert retry_after_s(b, NOW + 60_000.5, NOW) == 1  # at least 1
    huge = admission_bucket("key:k:rpm", "requests", 10, 11)
    assert retry_after_s(huge, NOW + 66_000, NOW) == 60  # n > limit never passes


def test_labeling_headroom_lowers_the_threshold() -> None:
    b = admission_bucket("mdl:t:a:rpm", "requests", 10, 8, headroom=LABELING_HEADROOM)
    assert not denied(b, NOW + 8 * 6000, NOW)  # 80% used: allowed
    assert denied(b, NOW + 9 * 6000, NOW)


def test_divided_keeps_tau_and_shrinks_rate_and_burst() -> None:
    b = admission_bucket("key:k:rpm", "requests", 10, 2).divided(4)
    assert (b.limit, b.interval_ms, b.tau_ms, b.n) == (2, 30_000.0, 60_000.0, 2)
    assert admission_bucket("x", "requests", 3, 1).divided(4).limit == 1  # never below 1
    r = rate_bucket("authfail:ip", 10.0, 50, 1).divided(5)
    assert (r.limit, r.tau_ms, r.interval_ms) == (10, 5000.0, 500.0)  # 2/s, burst 10
    assert Slot("s", 7, ("m",)).divided(2).cap == 3 and Slot("s", 1, ("m",)).divided(4).cap == 1
    assert admission_bucket("x", "requests", 3, 1).divided(1).limit == 3


def test_local_store_all_or_nothing() -> None:
    store = LocalStore()
    ok = admission_bucket("a:rpm", "requests", 10, 5)
    too_much = admission_bucket("b:tpm", "tokens", 100, 101)
    verdict = store.reserve([ok, too_much], [], NOW + 1, NOW)
    assert verdict.status == "rate" and store.tat == {}
    assert verdict.before == (NOW, NOW) and verdict.after == (NOW + 30_000, NOW + 101 * 600)
    assert store.reserve([ok], [], NOW + 1, NOW).status == "ok"
    assert store.tat == {"a:rpm": NOW + 30_000}


def test_local_store_check_only_evaluates_the_buckets_and_writes_nothing() -> None:
    store = LocalStore()
    ok = admission_bucket("a:rpm", "requests", 10, 5)
    full_slot = Slot("streams:t:p", 0, ("r1",))  # slots are not looked at
    checked = store.reserve([ok], [full_slot], NOW + 1, NOW, check_only=True)
    assert checked == store.reserve([ok], [], NOW + 1, NOW) and checked.status == "ok"  # the same verdict
    assert store.tat == {"a:rpm": NOW + 30_000}  # only the real reserve wrote
    denied_check = store.reserve([ok.with_n(6)], [], NOW + 1, NOW, check_only=True)
    assert denied_check.status == "rate" and store.tat == {"a:rpm": NOW + 30_000} and store.slots == {}


def test_slot_limit_keeps_the_labeling_headroom_also_per_pod() -> None:
    assert [Slot("k", cap, ("m",), LABELING_HEADROOM).limit for cap in (1, 2, 5, 10, 15, 35)] == [0, 1, 4, 8, 12, 28]
    assert Slot("k", 7, ("m",)).limit == 7
    shared = Slot("k", 40, ("m",), LABELING_HEADROOM).divided(4)
    assert (shared.cap, shared.limit) == (10, 8)
    store = LocalStore()
    assert store.reserve([], [Slot("k", 2, ("u1",))], NOW + 1, NOW).status == "ok"
    assert store.reserve([], [Slot("k", 2, ("l1",), LABELING_HEADROOM)], NOW + 1, NOW).status == "slot"  # 1 + 1 > 1
    assert store.reserve([], [Slot("k", 2, ("u2",))], NOW + 1, NOW).status == "ok"  # users may fill the cap


def test_local_store_refund_never_goes_below_now() -> None:
    store = LocalStore()
    b = admission_bucket("a:rpm", "requests", 10, 5)
    store.reserve([b], [], NOW, NOW)
    store.refund([b.with_n(2)], [], NOW)
    assert store.tat["a:rpm"] == NOW + 18_000
    store.refund([b.with_n(9)], [], NOW + 1000)
    assert "a:rpm" not in store.tat and store.peek("a:rpm", NOW + 1000) == NOW + 1000


def test_local_store_slots_expire_with_their_lease() -> None:
    store = LocalStore()
    slot = Slot("streams:t:p", 1, ("r1",))
    assert store.reserve([], [slot], NOW + 5000, NOW).status == "ok"
    second = store.reserve([admission_bucket("a:rpm", "requests", 10, 1)], [Slot("streams:t:p", 1, ("r2",))], NOW, NOW)
    assert (second.status, second.full_slot) == ("slot", 0) and store.tat == {}  # nothing debited
    assert store.reserve([], [Slot("streams:t:p", 1, ("r2",))], NOW + 9000, NOW + 5000).status == "ok"  # r1 expired
    store.refund([], [Slot("streams:t:p", 1, ("r2",))], NOW + 5000)
    assert store.slots["streams:t:p"] == {}


def test_local_store_prunes_refilled_entries() -> None:
    store = LocalStore()
    for i in range(5000):
        store.reserve([admission_bucket(f"k{i}:rpm", "requests", 600, 1)], [], NOW, NOW + i)
    assert len(store.tat) < 5000  # buckets refilled long ago were dropped
    assert store.peek("k4999:rpm", NOW + 4999) == NOW + 4999 + 100


def test_quota_buckets_are_windows_whose_cap_divides_per_pod() -> None:
    b = quota_bucket("mdl:t:a:rpm", "requests", 600, 3, alias="a")
    assert (b.slices, b.cap, QUOTA_SLICES) == (62, 600, 62)  # 60 s + 1 s margin + the current slice
    labeling = quota_bucket("mdl:t:a:rpm", "requests", 600, 3, alias="a", headroom=LABELING_HEADROOM)
    assert labeling.cap == 480 and labeling.divided(4).cap == 120 and b.divided(4).cap == 150
    assert b.divided(4000).cap == 1 and b.with_n(1).slices == 62  # never below 1
    store = LocalStore()
    assert store.reserve([b.with_n(600)], [], NOW, NOW).status == "ok"
    verdict = store.reserve([b.with_n(1)], [], NOW, NOW + 60_999)  # NOW is on a slice boundary: 62 s
    assert (verdict.status, verdict.before, verdict.waits) == ("rate", (600.0,), (1001.0,))
    assert store.reserve([b.with_n(600)], [], NOW, NOW + 62_000).status == "ok"
