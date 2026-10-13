"""`GcraAdmission` and `GcraRateLimiter` on the compose Valkey (spec-11 §1.2, §8): atomic reservations under
concurrency from two pods, cross-pod stream slots and refunds on the Redis state."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import anyio
import pytest
import redis.asyncio
from tests.contract_internal.kits import principal
from tests.fakes.clock import FakeClock
from tests.fakes.config import FakeConfigRegistry, patched_example, snapshot_from

from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.ops import WorkGraph
from noema.core.redis_keys import RedisKeys
from noema.core.values import UsageRecord
from noema.ops.admission import GcraAdmission, GcraReservation

from .redis_support import redis_namespace

pytestmark = pytest.mark.integration
TIMEOUT_S = 2.0  # the per-call Redis bound for these tests (the 50 ms default is exercised in test_failover)


def graph(calls: int = 1, tokens: int = 10, streams: int = 0) -> WorkGraph:
    return WorkGraph(
        {("sol-6.1", "generation"): calls}, {"sol-6.1": tokens}, calls, tokens // 2, tokens - tokens // 2, streams
    )


def registry(**limits: Any) -> FakeConfigRegistry:
    return FakeConfigRegistry(snapshot_from(patched_example({"tenants": {"t_acme": {"limits": limits}}})))


Pod = Callable[[FakeClock, FakeConfigRegistry], GcraAdmission]


@pytest.fixture
async def shared(noema_services: Any, noema_run_id: str) -> AsyncIterator[tuple[redis.asyncio.Redis, RedisKeys, Pod]]:
    """The test's namespace on the shared Valkey and a pod factory (each pod: its own connection pool)."""
    clients: list[redis.asyncio.Redis] = []

    def pod(clock: FakeClock, config: FakeConfigRegistry) -> GcraAdmission:
        client = redis.asyncio.Redis.from_url(noema_services.redis_url, decode_responses=False)
        clients.append(client)
        return GcraAdmission(clock, config, keys, client, env="test", redis_timeout_s=TIMEOUT_S)

    async with redis_namespace(noema_services.redis_url, noema_run_id) as (client, keys):
        try:
            yield client, keys, pod
        finally:
            for c in clients:
                await c.aclose()


async def test_concurrent_reservations_from_two_pods_never_exceed_the_limit(shared: Any) -> None:
    client, keys, pod = shared
    clock, config = FakeClock(), registry(rpm_per_key=20)
    pods = [pod(clock, config), pod(clock, config)]
    admitted: list[GcraReservation] = []
    rejected: list[ErrorCode] = []

    async def one(i: int) -> None:
        try:
            admitted.append(await pods[i % 2].reserve(principal(), graph(calls=1), queue_timeout_s=0))
        except NoemaError as err:
            rejected.append(err.code)

    async with anyio.create_task_group() as tg:
        for i in range(50):
            tg.start_soon(one, i)
    assert len(admitted) == 20 and rejected == [ErrorCode.RPM_EXCEEDED] * 30
    assert not any(p.degraded for p in pods)
    tat = float(await client.get(keys.gcra("key:key_1:rpm")))
    assert tat - clock.now().timestamp() * 1000 == pytest.approx(20 * 3000, abs=1)  # one shared bucket, full


async def test_concurrent_stream_slots_are_atomic_across_pods(shared: Any) -> None:
    client, keys, pod = shared
    clock, config = FakeClock(), registry(concurrent_streams_per_project=5)
    pods = [pod(clock, config), pod(clock, config)]
    admitted: list[GcraReservation] = []

    async def one(i: int) -> None:
        try:
            admitted.append(await pods[i % 2].reserve(principal(), graph(streams=1), queue_timeout_s=0))
        except NoemaError as err:
            assert err.code is ErrorCode.QUEUE_TIMEOUT

    async with anyio.create_task_group() as tg:
        for i in range(20):
            tg.start_soon(one, i)
    slot_key = keys.streams("t_acme", "p1")
    assert len(admitted) == 5 and await client.zcard(slot_key) == 5
    for res in admitted:
        await res.release()
    assert await client.zcard(slot_key) == 0


async def test_a_release_on_one_pod_admits_a_waiter_on_another(shared: Any) -> None:
    _client, _keys, pod = shared
    clock, config = FakeClock(), registry(concurrent_streams_per_project=1)
    pod_a, pod_b = pod(clock, config), pod(clock, config)
    holder = await pod_a.reserve(principal(), graph(streams=1), queue_timeout_s=0)
    got: list[float] = []
    async with clock.autojump(), anyio.create_task_group() as tg:

        async def waiter() -> None:
            await pod_b.reserve(principal(), graph(streams=1), queue_timeout_s=5)
            got.append(clock.monotonic())

        start = clock.monotonic()
        tg.start_soon(waiter)
        await clock.sleep(1.0)
        assert not got
        await holder.release()  # pod B only sees it at its next 100 ms poll
    assert 1.0 <= got[0] - start <= 1.2


async def test_reconcile_refunds_on_the_shared_buckets(shared: Any) -> None:
    client, keys, pod = shared
    clock, config = FakeClock(), registry(rpm_per_key=10, tpm_per_key=1000)
    adm = pod(clock, config)
    res = await adm.reserve(principal(), graph(calls=10, tokens=1000), queue_timeout_s=0)
    res.consume("generation", "sol-6.1")
    await res.reconcile({"sol-6.1": UsageRecord(150, 50)})
    await res.release()
    rpm, tpm = await client.get(keys.gcra("key:key_1:rpm")), await client.get(keys.gcra("key:key_1:tpm"))
    now = (clock.now().timestamp()) * 1000
    assert float(rpm) - now == pytest.approx(1 * 6000, abs=1)  # 1 consumed call of 10 stays debited
    assert float(tpm) - now == pytest.approx(200 * 60, abs=1)  # 200 actual tokens of 1000
    other = pod(clock, config)
    await other.reserve(principal(), graph(calls=9, tokens=800), queue_timeout_s=0)  # the refunds are shared
    with pytest.raises(NoemaError):
        await other.reserve(principal(), graph(calls=1, tokens=0), queue_timeout_s=0)
