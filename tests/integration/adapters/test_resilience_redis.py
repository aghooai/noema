"""Cross-pod sync of the breakers and the retry budget over the compose Valkey (spec-02 §14 `test_health_registry.py`
"2 processes over Redis pub/sub", IMPL-078): two registries in one process stand for two pods (separate clients),
plus Redis-down behavior (local only, no exception escapes the sync tasks). Real time, short policies."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Callable

import anyio
import pytest
import redis.asyncio as aioredis

from noema.adapters.resilience.breakers import BreakerPolicy, BreakerRegistry
from noema.adapters.resilience.retry_budget import RetryBudgetPolicy, SlidingRetryBudget
from noema.core.clock import SystemClock
from noema.core.redis_keys import RedisKeys

pytestmark = pytest.mark.integration
M, R, A = "us.openai.gpt-6.1-sol", "us-east-1", "111122223333"


@pytest.fixture
async def keys(noema_services: object, noema_run_id: str) -> AsyncIterator[tuple[str, RedisKeys]]:
    url = noema_services.redis_url  # type: ignore[attr-defined]
    probe = aioredis.from_url(url)
    try:
        await probe.ping()
    except Exception:
        await probe.aclose()
        pytest.skip("the compose Valkey is not reachable")
    ns = f"ns:{noema_run_id}:{uuid.uuid4().hex[:8]}:"
    try:
        yield url, RedisKeys(ns)
    finally:
        async for key in probe.scan_iter(match=f"{ns}*"):
            await probe.unlink(key)
        await probe.aclose()


async def eventually(check: Callable[[], bool], timeout_s: float = 5.0) -> None:
    end = time.monotonic() + timeout_s
    while not check():
        assert time.monotonic() < end, "condition not reached in time"
        await anyio.sleep(0.02)


async def test_breaker_transitions_reach_the_other_pod(keys: tuple[str, RedisKeys]) -> None:
    url, rk = keys
    clock = SystemClock()
    policy = BreakerPolicy(open_s=0.6)
    pod_a, pod_b = BreakerRegistry(clock, policy=policy, keys=rk), BreakerRegistry(clock, policy=policy, keys=rk)
    redis_a, redis_b = aioredis.from_url(url), aioredis.from_url(url)
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(pod_a.run, redis_a)
            tg.start_soon(pod_b.run, redis_b)
            await anyio.sleep(0.3)  # both subscribed
            for _ in range(20):
                pod_a.record(M, R, A, "error", None)
            await eventually(lambda: pod_b.state(M, R) == "open")
            assert not pod_b.admit(M, R, A)
            for _ in range(20):
                pod_a.record(M, R, "999988887777", "throttled", None)
            await eventually(lambda: not pod_b.admit(M, R, "999988887777"))
            await eventually(lambda: pod_a.state(M, R) == "half_open")
            assert pod_a.admit(M, R, A)  # the probe, admitted on pod a
            pod_a.record(M, R, A, "ok", 300)  # the probe passes on pod a
            await eventually(lambda: pod_b.state(M, R) == "closed")
            tg.cancel_scope.cancel()
    finally:
        await redis_a.aclose()
        await redis_b.aclose()


async def test_retry_budget_counts_the_other_pods_requests(keys: tuple[str, RedisKeys]) -> None:
    url, rk = keys
    clock = SystemClock()
    policy = RetryBudgetPolicy(min_retries=0, sync_s=0.05)
    pod_a = SlidingRetryBudget(clock, policy=policy, keys=rk, pod_id="pod-a")
    pod_b = SlidingRetryBudget(clock, policy=policy, keys=rk, pod_id="pod-b")
    redis_a, redis_b = aioredis.from_url(url), aioredis.from_url(url)
    try:
        assert not pod_b.try_spend(A, M)  # no first attempts anywhere yet
        for _ in range(200):
            pod_a.note_request(A, M)
        async with anyio.create_task_group() as tg:
            tg.start_soon(pod_a.run, redis_a)
            tg.start_soon(pod_b.run, redis_b)
            await eventually(lambda: pod_b.try_spend(A, M))
            fields = await redis_a.hgetall(rk.retry_budget(A, M))
            assert set(fields) == {b"pod-a", b"pod-b"}
            tg.cancel_scope.cancel()
    finally:
        await redis_a.aclose()
        await redis_b.aclose()


async def test_redis_down_keeps_both_local_and_never_raises() -> None:
    clock = SystemClock()
    keys = RedisKeys(f"ns:down:{uuid.uuid4().hex[:8]}:")
    dead = aioredis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    breakers = BreakerRegistry(clock, keys=keys)
    budget = SlidingRetryBudget(clock, policy=RetryBudgetPolicy(sync_s=0.05), keys=keys)
    try:
        with anyio.move_on_after(0.8):
            async with anyio.create_task_group() as tg:
                tg.start_soon(breakers.run, dead)
                tg.start_soon(budget.run, dead)
                for _ in range(20):
                    breakers.record(M, R, A, "error", None)
                    budget.note_request(A, M)  # R8: 20 first sends allow 2 re-sends
                assert breakers.state(M, R) == "open"
                assert budget.try_spend(A, M)
                await anyio.sleep(10)
    finally:
        await dead.aclose()
