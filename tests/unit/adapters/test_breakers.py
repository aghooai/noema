"""The circuit-breaker state machine (`BreakerRegistry`, design 04 §2, spec-10 F3): the >= 20 requests / > 50 %
rule on a rolling 30 s window, half-open after 15 s with 5 % probes, probe success/failure, the per-account quota
breaker fed by throttles, the p95 TTFT rule against the baseline, the state gauge, and the pub/sub payloads both
ways (publish on local transitions, apply remote ones)."""

from __future__ import annotations

import logging
from typing import Literal

import orjson
import pytest
from tests.fakes.clock import FakeClock
from tests.fakes.telemetry import FakeTelemetry

from noema.adapters.resilience.breakers import BreakerPolicy, BreakerRegistry
from noema.core.redis_keys import RedisKeys

M, R, A, B = "us.openai.gpt-6.1-sol", "us-east-1", "111122223333", "444455556666"


def registry(clock: FakeClock, *, sync: bool = False) -> BreakerRegistry:
    return BreakerRegistry(clock, telemetry=FakeTelemetry(), keys=RedisKeys("ns:t:") if sync else None)


def record(h: BreakerRegistry, outcome: str, n: int, ttft: int | None = None) -> None:
    for _ in range(n):
        h.record(M, R, A, outcome, ttft)  # type: ignore[arg-type]


async def test_exactly_half_errors_stays_closed_one_more_opens() -> None:
    h = registry(FakeClock())
    record(h, "ok", 10, 500)
    record(h, "error", 10)
    assert h.state(M, R) == "closed"
    record(h, "timeout", 1)
    assert h.state(M, R) == "open" and not h.admit(M, R, A)


async def test_needs_twenty_requests() -> None:
    h = registry(FakeClock())
    record(h, "error", 19)
    assert h.state(M, R) == "closed" and h.admit(M, R, A)


async def test_old_outcomes_leave_the_window() -> None:
    clock = FakeClock()
    h = registry(clock)
    record(h, "error", 15)
    await clock.advance(31)
    record(h, "error", 15)
    assert h.state(M, R) == "closed"
    record(h, "error", 5)
    assert h.state(M, R) == "open"


async def test_half_open_admits_five_percent_probes() -> None:
    clock = FakeClock()
    h = registry(clock)
    record(h, "error", 20)
    await clock.advance(14.9)
    assert h.state(M, R) == "open"
    await clock.advance(0.1)
    assert h.state(M, R) == "half_open"
    admitted = [h.admit(M, R, A) for _ in range(40)]
    assert admitted.count(True) == 2 and admitted[0] and admitted[20]


async def test_an_admitted_probe_closes_on_ok_and_reopens_on_error() -> None:
    clock = FakeClock()
    h = registry(clock)
    record(h, "error", 20)
    await clock.advance(15)
    assert h.admit(M, R, A)  # the probe
    record(h, "error", 1)
    assert h.state(M, R) == "open"
    await clock.advance(15)
    assert h.state(M, R) == "half_open" and h.admit(M, R, A)
    record(h, "ok", 1, 400)
    assert h.state(M, R) == "closed" and h.admit(M, R, A)
    record(h, "error", 19)
    assert h.state(M, R) == "closed"  # the window restarted at the close


@pytest.mark.parametrize("late", ["ok", "error"])
async def test_a_late_outcome_of_a_call_admitted_before_the_open_never_decides_the_probe(
    late: Literal["ok", "error"],
) -> None:
    """Wave E (design 04 §2: the probe decides): a long stream admitted while closed and ending in half-open, with
    no probe sent, neither closed the breaker during the burst ('ok') nor re-opened it for another 15 s ('error')."""
    clock = FakeClock()
    h = registry(clock)
    assert h.admit(M, R, B)  # a long stream, admitted while closed
    record(h, "error", 20)
    await clock.advance(5)
    record(h, "throttled", 20)  # A's quota breaker opens 5 s later than the service breaker
    await clock.advance(10)
    assert h.state(M, R) == "half_open" and not h.admit(M, R, A)  # refused by A's quota breaker: no probe sent
    h.record(M, R, B, late, 400 if late == "ok" else None)  # B's pre-open stream ends
    assert h.state(M, R) == "half_open"
    admitted = [h.admit(M, R, B) for _ in range(20)]  # A's refused call took the first probe turn
    assert admitted.count(True) == 1 and admitted[-1]
    h.record(M, R, B, "ok", 400)
    assert h.state(M, R) == "closed"


async def test_throttles_open_only_that_accounts_quota_breaker() -> None:
    clock = FakeClock()
    h = registry(clock)
    record(h, "throttled", 20)
    assert h.state(M, R) == "closed"
    assert not h.admit(M, R, A) and h.admit(M, R, B) and h.admit("us.zai.glm-5.3", R, A)
    await clock.advance(15)
    assert h.admit(M, R, A)  # the half-open probe
    h.record(M, R, A, "ok", 300)
    assert all(h.admit(M, R, A) for _ in range(5))


async def test_ok_traffic_with_occasional_throttles_stays_open_for_others() -> None:
    h = registry(FakeClock())
    for i in range(40):
        h.record(M, R, A, "throttled" if i % 3 == 0 else "ok", 300)
    assert h.admit(M, R, A)


async def test_p95_ttft_above_three_times_the_baseline_opens() -> None:
    clock = FakeClock()
    h = registry(clock)
    for _ in range(100):  # ~3 windows of a 500 ms baseline, one outcome per second
        h.record(M, R, A, "ok", 500)
        await clock.advance(1)
    assert h.state(M, R) == "closed"
    opened_after = None
    for i in range(10):
        h.record(M, R, A if i % 2 else B, "ok", 2000)  # both accounts see the slowdown
        await clock.advance(1)
        if h.state(M, R) == "open":
            opened_after = i
            break
    assert opened_after is not None and opened_after <= 4


async def _armed(policy: BreakerPolicy | None = None) -> tuple[FakeClock, BreakerRegistry]:
    """A registry whose TTFT rule is armed: 100 s of 500 ms samples from accounts A and B."""
    clock = FakeClock()
    h = BreakerRegistry(clock, policy=policy)
    for i in range(100):
        h.record(M, R, A if i % 2 else B, "ok", 500)
        await clock.advance(1)
    return clock, h


async def test_one_accounts_slow_calls_never_open_the_shared_breaker_by_the_ttft_rule() -> None:
    """io #10 (rest): the p95-TTFT rule needs breaching samples from >= `ttft_min_accounts` (2) distinct tenant
    accounts; one tenant's slow calls alone (its prompts, its effort) cannot open the breaker every tenant shares."""
    clock, h = await _armed()
    for _ in range(40):  # 40 s of A's slow calls (4x the baseline), B quiet
        h.record(M, R, A, "ok", 2000)
        await clock.advance(1)
    assert h.state(M, R) == "closed" and h.admit(M, R, B)
    h.record(M, R, B, "ok", 2000)  # a second account breaches too
    await clock.advance(1)
    h.record(M, R, A, "ok", 2000)  # the next bucket runs the rule
    assert h.state(M, R) == "open"


async def _two_processes(
    slow_one: str, slow_two: str, *, armed_two: bool = True
) -> tuple[list[dict[str, object]], BreakerRegistry, BreakerRegistry]:
    """Two api processes on one baseline of both accounts (process two without traffic, so unarmed, when not
    `armed_two`), then each sees one account's slow calls (keep-alive connections pin a tenant to a process, and new
    connections of a slowed client land on the idle process: F3b on two processes per pod); their events are
    exchanged every second (returned, in order)."""
    clock, sent = FakeClock(), []
    one, two = BreakerRegistry(clock, keys=RedisKeys("ns:t:")), BreakerRegistry(clock, keys=RedisKeys("ns:t:"))
    for i in range(100):
        for h in (one, two) if armed_two else (one,):
            h.record(M, R, A if i % 2 else B, "ok", 500)
        await clock.advance(1)
    for h in (one, two):
        h._outbox.clear()
    for _ in range(10):
        one.record(M, R, slow_one, "ok", 2000)
        two.record(M, R, slow_two, "ok", 2000)
        await clock.advance(1)
        for src, dst in ((one, two), (two, one)):
            while src._outbox:
                sent.append(orjson.loads(src._outbox[0]))
                assert dst.apply_event(src._outbox.popleft())
    return sent, one, two


@pytest.mark.parametrize("armed_two", [True, False], ids=["both-armed", "second-unarmed"])
async def test_breach_evidence_of_other_processes_completes_the_account_quorum(armed_two: bool) -> None:
    """Each process keeps its own window: one whose window breaches without the quorum publishes its breaching
    accounts and its limit (`ttft_breach`, valid for one window); every process counts the fresh remote accounts and
    answers with its own accounts above that limit, so an unarmed process (no baseline yet) contributes too."""
    _, one, two = await _two_processes(A, B, armed_two=armed_two)
    assert one.state(M, R) == "open" and two.state(M, R) == "open"


@pytest.mark.parametrize("late", [False, True], ids=["in-time", "late"])
async def test_a_pending_breach_opens_as_the_answer_arrives_without_new_traffic(late: bool) -> None:
    """F3b on two processes per pod: the armed process breaches on one account, then its clients' new connections
    land on the idle (unarmed) process, so it gets no further sample to re-check on; the answer completes its
    pending breach as it arrives, unless it comes more than `_BREACH_PENDING_S` (5 s) after the breach."""
    clock = FakeClock()
    one, two = BreakerRegistry(clock, keys=RedisKeys("ns:t:")), BreakerRegistry(clock, keys=RedisKeys("ns:t:"))
    for i in range(100):
        one.record(M, R, A if i % 2 else B, "ok", 500)
        await clock.advance(1)
    one._outbox.clear()
    for _ in range(3):
        one.record(M, R, B, "ok", 2000)
        await clock.advance(1)
    one.record(M, R, B, "ok", 2000)  # the checks of these new buckets breach on B alone and ask
    assert one.state(M, R) == "closed" and one._outbox
    while one._outbox:
        assert two.apply_event(one._outbox.popleft())
    for _ in range(2):  # A's calls now reach process two only
        two.record(M, R, A, "ok", 2000)
        await clock.advance(1)
    two.record(M, R, A, "ok", 2000)
    assert two._outbox and all(orjson.loads(e)["accounts"] == [A] for e in two._outbox)
    if late:
        await clock.advance(4)
    assert one.apply_event(two._outbox.pop())
    assert one.state(M, R) == ("closed" if late else "open")


async def test_an_ask_is_answered_at_once_from_the_current_window() -> None:
    """The answering process does not wait for its next sample (up to a second or more at low traffic, which delayed
    F3b's opens and re-opens past the restore): it answers as the ask arrives, and the asker opens as the answer
    arrives."""
    clock = FakeClock()
    one, two = BreakerRegistry(clock, keys=RedisKeys("ns:t:")), BreakerRegistry(clock, keys=RedisKeys("ns:t:"))
    for i in range(100):
        one.record(M, R, A if i % 2 else B, "ok", 500)
        await clock.advance(1)
    one._outbox.clear()
    for _ in range(3):
        one.record(M, R, B, "ok", 2000)
        two.record(M, R, A, "ok", 2000)  # unarmed, not asked yet: no event
        await clock.advance(1)
    one.record(M, R, B, "ok", 2000)
    assert not two._outbox and one._outbox and one.state(M, R) == "closed"
    assert two.apply_event(one._outbox.pop())
    [answer] = [orjson.loads(e) for e in two._outbox]
    assert answer["accounts"] == [A] and answer["limit_ms"] == 1500.0
    assert one.apply_event(two._outbox.popleft())
    assert one.state(M, R) == "open"


async def test_one_account_slow_on_every_process_still_never_opens() -> None:
    """One ask (A, the limit, one window), never repeated within the window; process two has no account above the
    limit beyond the reported one, so it never answers and nothing opens."""
    sent, one, two = await _two_processes(A, A, armed_two=False)
    assert one.state(M, R) == "closed" and two.state(M, R) == "closed"
    [event] = sent
    assert event == {
        "model": M, "region": R, "state": "ttft_breach", "accounts": [A], "limit_ms": 1500.0,
        "until_ms": event["until_ms"], "origin": one.origin,
    }  # fmt: skip
    assert isinstance(event["until_ms"], int)


async def test_the_ttft_rule_never_reopens_on_samples_from_before_a_restore() -> None:
    """F3b (fault.md §5 flapping, 2 processes per pod): a half-open probe closes the breaker while the model is still
    slow, the cleared window refills with slow samples, the latency is restored before it holds 20, and the first
    fast samples' checks saw only the stale slow window (the incoming sample was not in it): the breaker re-opened
    after the recovery and refused healthy traffic for another `open_s`. The rule now needs the incoming sample above
    the limit too."""
    clock, h = await _armed()
    for i in range(10):
        h.record(M, R, A if i % 2 else B, "ok", 2000)
        await clock.advance(1)
    assert h.state(M, R) == "open"
    await clock.advance(15)
    assert h.admit(M, R, A)
    h.record(M, R, A, "ok", 2000)  # the probe answered: closed, window cleared, still slow
    assert h.state(M, R) == "closed"
    for i in range(19):
        await clock.advance(1)
        h.record(M, R, A if i % 2 else B, "ok", 2000)
    for i in range(10):  # restored
        await clock.advance(1)
        h.record(M, R, A if i % 2 else B, "ok", 500)
        assert h.state(M, R) == "closed", i


async def test_the_ttft_rule_account_quorum_is_configurable() -> None:
    clock, h = await _armed(BreakerPolicy(ttft_min_accounts=1))
    for _ in range(10):
        h.record(M, R, A, "ok", 2000)
        await clock.advance(1)
    assert h.state(M, R) == "open"


async def test_steady_ttft_never_trips() -> None:
    clock = FakeClock()
    h = registry(clock)
    for i in range(300):
        h.record(M, R, A, "ok", 400 + (i % 7) * 50)
        await clock.advance(0.5)
    assert h.state(M, R) == "closed"


async def test_state_gauge() -> None:
    clock = FakeClock()
    telemetry = FakeTelemetry()
    h = BreakerRegistry(clock, telemetry=telemetry)
    record(h, "error", 20)
    await clock.advance(15)
    assert h.admit(M, R, A)  # half-open: the probe
    record(h, "ok", 1, 100)
    gauge = [(m.value, dict(m.labels)) for m in telemetry.metrics if m.name == "aghoo_circuit_state"]
    assert [v for v, _ in gauge] == [2, 1, 0] and gauge[0][1] == {"model": M, "region": R}


async def test_the_open_warning_names_the_model_and_region(caplog: pytest.LogCaptureFixture) -> None:
    # the JSON log formatter drops the extras: the text itself says which circuit opened (model ids are not content)
    h = registry(FakeClock())
    with caplog.at_level(logging.WARNING, logger="noema.adapters.breakers"):
        record(h, "error", 20)
        for _ in range(20):
            h.record(M, R, A, "throttled", None)
    assert [r.getMessage() for r in caplog.records] == [f"circuit open: {M} {R}", f"quota circuit open: {M} {R}"]
    assert all(A not in r.getMessage() for r in caplog.records)  # never the tenant account


async def test_local_transitions_are_published() -> None:
    clock = FakeClock()
    h = registry(clock, sync=True)
    record(h, "error", 20)
    record(h, "throttled", 20)
    await clock.advance(15)
    assert h.admit(M, R, A)  # half-open: the probe of both breakers
    record(h, "ok", 1, 300)
    events = [orjson.loads(e) for e in h._outbox]
    wall_ms = round(clock.now().timestamp() * 1000)
    assert events[0] == {"model": M, "region": R, "state": "open", "until_ms": wall_ms, "origin": h.origin}
    assert events[1]["tenant_account"] == A and events[1]["state"] == "open"
    assert {"model": M, "region": R, "state": "closed", "origin": h.origin} in events


async def test_remote_open_applies_until_its_end_and_remote_close_releases_it() -> None:
    clock = FakeClock()
    h = registry(clock)
    until = round(clock.now().timestamp() * 1000) + 10_000
    assert h.apply_event(orjson.dumps({"model": M, "region": R, "state": "open", "until_ms": until}))
    assert h.state(M, R) == "open" and not h.admit(M, R, A)
    await clock.advance(10)
    assert h.state(M, R) == "half_open"
    assert h.apply_event(orjson.dumps({"model": M, "region": R, "state": "closed"}))
    assert h.state(M, R) == "closed"


async def test_remote_close_never_overrides_local_evidence() -> None:
    h = registry(FakeClock())
    record(h, "error", 20)
    assert h.apply_event(orjson.dumps({"model": M, "region": R, "state": "closed"}))
    assert h.state(M, R) == "open"


async def test_own_echo_is_ignored_so_local_evidence_stays_local() -> None:
    # adapters review: a pod subscribes to the channel it publishes on; its own `open` must not turn remote
    clock = FakeClock()
    h = registry(clock, sync=True)
    record(h, "error", 20)
    echo = orjson.loads(h._outbox[0])
    echo["until_ms"] += 1  # ms rounding and wall vs monotonic drift: the echo's end is often slightly later
    assert h.apply_event(orjson.dumps(echo))
    assert not h._service[(M, R)].remote
    assert h.apply_event(orjson.dumps({"model": M, "region": R, "state": "closed", "origin": "other-pod"}))
    assert h.state(M, R) == "open"


async def test_a_later_remote_open_extends_a_local_one_and_keeps_it_local() -> None:
    clock = FakeClock()
    h = registry(clock)
    record(h, "error", 20)  # open locally until +15 s
    until = round(clock.now().timestamp() * 1000) + 20_000
    assert h.apply_event(orjson.dumps({"model": M, "region": R, "state": "open", "until_ms": until, "origin": "b"}))
    assert h.apply_event(orjson.dumps({"model": M, "region": R, "state": "closed", "origin": "c"}))
    await clock.advance(16)
    assert h.state(M, R) == "open"  # extended to +20 s, and the remote close did not override local evidence
    await clock.advance(4)
    assert h.state(M, R) == "half_open"


async def test_remote_quota_open_blocks_only_that_account() -> None:
    clock = FakeClock()
    h = registry(clock)
    until = round(clock.now().timestamp() * 1000) + 5000
    event = {"model": M, "region": R, "tenant_account": A, "state": "open", "until_ms": until}
    assert h.apply_event(orjson.dumps(event))
    assert not h.admit(M, R, A) and h.admit(M, R, B) and h.state(M, R) == "closed"


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[1]",
        orjson.dumps({"model": M, "state": "open", "until_ms": 1}),
        orjson.dumps({"model": M, "region": R, "state": "exploded"}),
        orjson.dumps({"model": M, "region": R, "state": "open", "until_ms": True}),
        orjson.dumps({"model": M, "region": R, "state": "open", "tenant_account": 7, "until_ms": 1}),
        orjson.dumps({"model": M, "region": R, "state": "ttft_breach", "accounts": A, "limit_ms": 9, "until_ms": 1}),
        orjson.dumps({"model": M, "region": R, "state": "ttft_breach", "accounts": [7], "limit_ms": 9, "until_ms": 1}),
        orjson.dumps({"model": M, "region": R, "state": "ttft_breach", "accounts": [A], "limit_ms": 9}),
        orjson.dumps({"model": M, "region": R, "state": "ttft_breach", "accounts": [A], "until_ms": 1}),
        orjson.dumps(
            {"model": M, "region": R, "state": "ttft_breach", "accounts": [A], "limit_ms": True, "until_ms": 1}
        ),
        orjson.dumps({"model": M, "region": R, "tenant_account": A, "state": "ttft_breach", "accounts": [A]}),
    ],
)
def test_malformed_events_are_ignored(raw: bytes) -> None:
    h = registry(FakeClock())
    assert not h.apply_event(raw)
    assert h.state(M, R) == "closed"


async def test_expired_remote_open_is_a_no_op() -> None:
    clock = FakeClock()
    h = registry(clock)
    past = round(clock.now().timestamp() * 1000) - 1
    assert h.apply_event(orjson.dumps({"model": M, "region": R, "state": "open", "until_ms": past}))
    assert h.state(M, R) == "closed"
