"""`SlidingRetryBudget` (IMPL-016, IMPL-078, F12; owner ruling R8): 10 % of first sends per (tenant account, model)
per 60 s with no floor, the exempt connect retry of a send that never reached the upstream (not a first send, no
budget), the budgeted one after a possible delivery, the sliding window, isolation per key, and the coarse cross-pod
sync (other pods' fresh totals count, stale ones are ignored, idle keys are dropped) over an in-memory stand-in of
the Redis hash commands."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from tests.fakes.clock import FakeClock

from noema.adapters.resilience.retry_budget import RetryBudgetPolicy, SlidingRetryBudget, _parse_totals
from noema.core.redis_keys import RedisKeys

A, M = "111122223333", "us.openai.gpt-6.1-sol"
KEYS = RedisKeys("ns:t:")


class FakeHashRedis:
    """HSET / EXPIRE / HGETALL through `pipeline(transaction=False).execute()`, bytes like the real client."""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[bytes, bytes]] = {}
        self.ttls: dict[str, int] = {}

    def pipeline(self, transaction: bool = True) -> FakeHashRedis._Pipe:
        return FakeHashRedis._Pipe(self)

    class _Pipe:
        def __init__(self, owner: FakeHashRedis) -> None:
            self.owner, self.ops = owner, list[tuple[str, tuple[Any, ...]]]()

        def hset(self, key: str, field: str, value: str) -> None:
            self.ops.append(("hset", (key, field, value)))

        def expire(self, key: str, seconds: int) -> None:
            self.ops.append(("expire", (key, seconds)))

        def hgetall(self, key: str) -> None:
            self.ops.append(("hgetall", (key,)))

        async def execute(self) -> list[Any]:
            out: list[Any] = []
            for op, args in self.ops:
                if op == "hset":
                    self.owner.hashes.setdefault(args[0], {})[args[1].encode()] = args[2].encode()
                    out.append(1)
                elif op == "expire":
                    self.owner.ttls[args[0]] = args[1]
                    out.append(True)
                else:
                    out.append(dict(self.owner.hashes.get(args[0], {})))
            return out


def spend_all(budget: SlidingRetryBudget, *, send: bool = True) -> int:
    granted = 0
    while budget.try_spend(A, M):
        granted += 1
        if send:
            budget.note_request(A, M)
        assert granted < 10_000
    return granted


def test_no_budget_without_traffic() -> None:
    assert spend_all(SlidingRetryBudget(FakeClock()), send=False) == 0  # R8: no floor


@pytest.mark.parametrize(("first_sends", "retries"), [(0, 0), (9, 0), (10, 1), (50, 5), (100, 10), (205, 20)])
def test_ten_percent_of_first_sends_at_every_traffic_level(first_sends: int, retries: int) -> None:
    # R8: N first sends per window allow floor(0.1 N) re-sends -> requests / first sends <= 1.1 for every N
    # (the IMPL-016 floor of 10 allowed 1 + 10 / N below N = 100: Sol 1.149 per upstream in F12 at rate 0.5)
    budget = SlidingRetryBudget(FakeClock())
    for _ in range(first_sends):
        budget.note_request(A, M)
    assert spend_all(budget) == retries
    assert first_sends + retries <= 1.1 * first_sends


def test_ten_percent_of_first_attempts() -> None:
    budget = SlidingRetryBudget(FakeClock(), policy=RetryBudgetPolicy(min_retries=0))
    for _ in range(1000):
        budget.note_request(A, M)
    assert spend_all(budget) == 100  # amplification (1000 + 100) / 1000 = 1.1


async def test_the_window_slides() -> None:
    clock = FakeClock()
    budget = SlidingRetryBudget(clock)
    for _ in range(100):
        budget.note_request(A, M)
    assert spend_all(budget) == 10
    await clock.advance(30)
    assert not budget.try_spend(A, M)
    for _ in range(50):
        budget.note_request(A, M)
    assert spend_all(budget) == 5  # 150 first sends in the window, 10 retries spent
    await clock.advance(31)  # the first 100 sends and their 10 retries left the window
    assert spend_all(budget) == 0  # the 50 sends' 5 retries are still in it


def test_keys_are_independent() -> None:
    budget = SlidingRetryBudget(FakeClock())
    for account, model in ((A, M), ("222222222222", M), (A, "us.zai.glm-5.3")):
        for _ in range(10):
            budget.note_request(account, model)
    spend_all(budget, send=False)
    assert budget.try_spend("222222222222", M) and budget.try_spend(A, "us.zai.glm-5.3")


def _adapter_asks(budget: SlidingRetryBudget, failure: Exception) -> bool:
    """The adapters' connect retry (`_base._send`): `try_spend` while handling the transport error."""
    try:
        raise failure
    except Exception:
        return budget.try_spend(A, M)


def test_a_connect_retry_that_never_reached_the_upstream_is_exempt_and_not_a_first_send() -> None:
    budget = SlidingRetryBudget(FakeClock())
    for _ in range(100):  # 100 calls whose first send failed to connect, each retried once
        budget.note_request(A, M)
        assert _adapter_asks(budget, httpx.ConnectError("refused"))  # granted on a quiet key: R8 exempt
        budget.note_request(A, M)
    assert spend_all(budget) == 10  # 100 first sends (the failed connects are not counted), 10 % of them


def test_a_connect_retry_after_a_possible_delivery_is_budgeted() -> None:
    budget = SlidingRetryBudget(FakeClock())
    budget.note_request(A, M)
    assert not _adapter_asks(budget, httpx.RemoteProtocolError("server disconnected"))  # 1 first send: no budget
    for _ in range(9):
        budget.note_request(A, M)
    assert _adapter_asks(budget, httpx.RemoteProtocolError("server disconnected"))  # 10 first sends: 1 re-send
    assert not _adapter_asks(budget, httpx.ReadError("reset"))
    assert not _adapter_asks(budget, ValueError("not a transport error"))  # any other cause is budgeted too


async def test_other_pods_fresh_totals_count_stale_ones_do_not() -> None:
    clock = FakeClock()
    redis = FakeHashRedis()
    policy = RetryBudgetPolicy(min_retries=0)
    pod_a = SlidingRetryBudget(clock, policy=policy, keys=KEYS, pod_id="a")
    pod_b = SlidingRetryBudget(clock, policy=policy, keys=KEYS, pod_id="b")
    for _ in range(500):
        pod_a.note_request(A, M)
    pod_b.note_request(A, M)
    await pod_a.sync_once(redis)  # type: ignore[arg-type]
    await pod_b.sync_once(redis)  # type: ignore[arg-type]
    assert redis.ttls[KEYS.retry_budget(A, M)] == 120
    assert set(redis.hashes[KEYS.retry_budget(A, M)]) == {b"a", b"b"}
    assert spend_all(pod_b) == 50  # 10 % of the 501 first attempts across both pods
    await clock.advance(6)  # pods a and b stopped syncing: their totals are stale now
    pod_c = SlidingRetryBudget(clock, policy=policy, keys=KEYS, pod_id="c")
    pod_c.note_request(A, M)
    await pod_c.sync_once(redis)  # type: ignore[arg-type]
    assert spend_all(pod_c) == 0


async def test_unsent_sends_are_not_published_as_first_sends() -> None:
    clock, redis = FakeClock(), FakeHashRedis()
    pod_a = SlidingRetryBudget(clock, keys=KEYS, pod_id="a")
    pod_b = SlidingRetryBudget(clock, keys=KEYS, pod_id="b")
    for _ in range(100):
        pod_a.note_request(A, M)
        assert _adapter_asks(pod_a, httpx.ConnectError("refused"))
    await pod_a.sync_once(redis)  # type: ignore[arg-type]
    assert redis.hashes[KEYS.retry_budget(A, M)][b"a"].startswith(b"0,0,")
    await pod_b.sync_once(redis)  # type: ignore[arg-type]
    assert spend_all(pod_b) == 0


async def test_idle_keys_are_dropped_and_sync_without_keys_is_a_no_op() -> None:
    clock = FakeClock()
    redis = FakeHashRedis()
    budget = SlidingRetryBudget(clock, keys=KEYS, pod_id="a")
    budget.note_request(A, M)
    await clock.advance(121)
    await budget.sync_once(redis)  # type: ignore[arg-type]
    assert redis.hashes == {}
    await SlidingRetryBudget(clock).run(redis)  # type: ignore[arg-type]  # keys None: returns at once


def test_parse_totals_rejects_garbage() -> None:
    assert _parse_totals("5,1,1000") == (5, 1, 1000)
    assert _parse_totals("5,1") is None and _parse_totals("a,b,c") is None and _parse_totals("-1,0,0") is None
