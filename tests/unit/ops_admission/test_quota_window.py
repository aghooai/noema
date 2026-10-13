"""QL-2 (Wave E2; C1 = 06 §5, spec-10 §3): a model quota (`mdl:{tenant}:{alias}:rpm|tpm`) never admits more than its
limit in any 60 s window, whatever the arrival pattern. The units that count are the ones a reservation keeps: the
calls it consumed and the tokens of its reconciled usage (refunds give back what was never sent to Bedrock).

Driven through `GcraAdmission` on the in-process store (the Redis script has the same semantics: T-GCRA-1/-3 in
tests/integration/ops_admission/test_lua_gcra.py). Key and project limits are lifted so that only the quota binds."""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.contract_internal.kits import principal
from tests.fakes.clock import FakeClock
from tests.fakes.config import FakeConfigRegistry, patched_example, snapshot_from

from noema.core.protocols.ops import WorkGraph
from noema.core.redis_keys import RedisKeys
from noema.core.values import UsageRecord
from noema.ops.admission import AdmissionRejected, GcraAdmission, GcraReservation

pytestmark = pytest.mark.unit
SOL: Final = "sol-6.1"
WINDOW_MS: Final = 60_000.0
LIFTED: Final = {
    "rpm_per_key": 10**9,
    "tpm_per_key": 10**12,
    "rpm_per_project": 10**9,
    "tpm_per_project": 10**12,
    "concurrent_streams_per_project": 10**6,
}


def admission(clock: FakeClock, rpm: int, tpm: int) -> GcraAdmission:
    limits = {**LIFTED, "model_quotas": {SOL: {"rpm": rpm, "tpm": tpm}}}
    config = FakeConfigRegistry(snapshot_from(patched_example({"tenants": {"t_acme": {"limits": limits}}})))
    return GcraAdmission(clock, config, RedisKeys("ns:unit:"), None, env="test")


def sol_graph(calls: int, tokens: int) -> WorkGraph:
    return WorkGraph({(SOL, "generation"): calls}, {SOL: tokens}, calls, tokens // 2, tokens - tokens // 2, 0)


@dataclass(frozen=True, slots=True)
class Arrival:
    gap_ms: float  # after the previous arrival
    calls: int  # reserved
    used_calls: int  # consumed (<= calls)
    tokens: int  # reserved
    used_tokens: int  # the reconciled usage (<= tokens)
    reconcile_after_ms: float


@dataclass(frozen=True, slots=True)
class Admitted:
    at_ms: float
    calls: int
    tokens: int


async def drive(rpm: int, tpm: int, arrivals: list[Arrival]) -> list[Admitted]:
    """Replay `arrivals` on a fresh admission; each admitted reservation consumes its calls and releases at once
    (the bulkhead place and the unused calls come back), and reconciles its token usage `reconcile_after_ms` later."""
    clock = FakeClock()
    adm = admission(clock, rpm, tpm)
    base, now = clock.monotonic(), 0.0
    due: list[tuple[float, int, GcraReservation, int]] = []
    admitted: list[Admitted] = []

    async def until(target_ms: float) -> None:
        while due and due[0][0] <= target_ms:
            at, _, res, used = heapq.heappop(due)
            await clock.advance_to(base + at / 1000.0)
            await res.reconcile({SOL: UsageRecord(used, 0)})
        await clock.advance_to(base + target_ms / 1000.0)

    for seq, a in enumerate(arrivals):
        now += a.gap_ms
        await until(now)
        try:
            res = await adm.reserve(principal(), sol_graph(a.calls, a.tokens), queue_timeout_s=0)
        except AdmissionRejected:
            continue
        for _ in range(a.used_calls):
            res.consume("generation", SOL)
        await res.release()
        heapq.heappush(due, (now + a.reconcile_after_ms, seq, res, a.used_tokens))
        admitted.append(Admitted(now, a.used_calls, a.used_tokens))
    await until(now + 10 * WINDOW_MS)
    return admitted


def busiest(admitted: list[Admitted], span_ms: float = WINDOW_MS) -> tuple[int, int]:
    """The most calls and the most tokens kept by the reservations admitted in any window [s, s + span)."""
    calls = tokens = 0
    for start in admitted:
        inside = [a for a in admitted if start.at_ms <= a.at_ms < start.at_ms + span_ms]
        calls = max(calls, sum(a.calls for a in inside))
        tokens = max(tokens, sum(a.tokens for a in inside))
    return calls, tokens


async def test_c1_the_first_minute_admits_at_most_the_rpm() -> None:
    """The C1 shape (QA lead §3.5: 915 Sol calls in the first minute against rpm 600): one Sol call every 10 ms
    for 130 s. Before the fix the GCRA's 60 s burst let 600 + 540 calls into the first minute."""
    arrivals = [Arrival(10.0, 1, 1, 100, 100, 0.0) for _ in range(13_000)]
    admitted = await drive(600, 10**11, arrivals)
    first = [a for a in admitted if a.at_ms < admitted[0].at_ms + WINDOW_MS]
    assert len(first) == 600
    assert busiest(admitted) == busiest(admitted, WINDOW_MS + 1000.0) == (600, 60_000)
    assert len(admitted) >= 1200  # and the quota is still used: a full window's worth again after ~1 minute


async def test_c1_tpm_is_bounded_the_same_way() -> None:
    arrivals = [Arrival(10.0, 1, 1, 1000, 1000, 0.0) for _ in range(13_000)]
    admitted = await drive(10**6, 100_000, arrivals)
    assert busiest(admitted)[1] == 100_000
    assert sum(a.tokens for a in admitted) >= 200_000


GAPS_MS: Final = (0.0, 1.0, 10.0, 100.0, 999.0, 1000.0, 1001.0, 5000.0, 30_000.0, 59_999.0, 60_000.0, 61_000.0)
arrival = st.builds(
    lambda gap, calls, kept, tokens, kept_tokens, after: Arrival(
        gap, calls, min(kept, calls), tokens, min(kept_tokens, tokens), after
    ),
    st.sampled_from(GAPS_MS),
    st.integers(0, 12),
    st.integers(0, 12),
    st.integers(0, 300),
    st.integers(0, 300),
    st.sampled_from([0.0, 500.0, 30_000.0, 70_000.0]),
)


@settings(max_examples=150)
@given(
    rpm=st.sampled_from([1, 7, 10, 60]),
    tpm=st.sampled_from([50, 1000]),
    arrivals=st.lists(arrival, min_size=1, max_size=60),
)
async def test_no_60s_window_admits_more_than_the_quota(rpm: int, tpm: int, arrivals: list[Arrival]) -> None:
    admitted = await drive(rpm, tpm, arrivals)
    calls, tokens = busiest(admitted)
    assert calls <= rpm and tokens <= tpm
    calls, tokens = busiest(admitted, WINDOW_MS + 1000.0)  # the 1 s issue-delay margin (gcra.QUOTA_MARGIN_MS)
    assert calls <= rpm and tokens <= tpm


async def test_a_full_quota_says_when_it_frees_and_frees_then() -> None:
    clock = FakeClock()  # DEFAULT_WALL is on a 1 s slice boundary: the units leave 62 s after their debit
    adm = admission(clock, 10, 10**6)
    res = await adm.reserve(principal(), sol_graph(10, 100), queue_timeout_s=0)
    for _ in range(10):
        res.consume("generation", SOL)
    await res.release()
    assert (res.headers()["x-ratelimit-remaining-requests"], res.headers()["x-ratelimit-reset-requests"]) == ("0", "62")
    await clock.advance(30.0)
    with pytest.raises(AdmissionRejected) as err:
        await adm.reserve(principal(), sol_graph(1, 1), queue_timeout_s=0)
    assert err.value.retry_after_s == 32 and err.value.headers["x-ratelimit-reset-requests"] == "32"
    await clock.advance(31.999)
    with pytest.raises(AdmissionRejected):
        await adm.reserve(principal(), sol_graph(1, 1), queue_timeout_s=0)
    await clock.advance(0.001)
    await adm.reserve(principal(), sol_graph(10, 1), queue_timeout_s=0)  # the whole quota again
