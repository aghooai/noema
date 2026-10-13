"""T-ADM-2 (spec-11 §8): Redis down -> per-pod local buckets at limit ÷ NOEMA_EXPECTED_PODS (admits, then 429 at
the local limit); Redis back -> new reservations use Redis again, local state set aside; a reply slower than 50 ms
counts as down; slot releases that failed in the outage are retried after it. Valkey is cut and slowed through a
session-owned proxy on the host toxiproxy; the down case also runs on a closed port when toxiproxy is not up."""

from __future__ import annotations

import os
import socket
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import anyio
import httpx
import pytest
import redis.asyncio
from tests.contract_internal.kits import principal
from tests.fakes.clock import FakeClock
from tests.fakes.config import FakeConfigRegistry, patched_example, snapshot_from
from tests.fakes.telemetry import FakeTelemetry

from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.ops import WorkGraph
from noema.core.redis_keys import RedisKeys
from noema.ops.admission import GcraAdmission, GcraRateLimiter

from .redis_support import redis_namespace

pytestmark = pytest.mark.integration
PODS = 4


@dataclass
class Proxy:
    api: httpx.Client
    name: str
    url: str

    def enabled(self, on: bool) -> None:
        self.api.post(f"/proxies/{self.name}", json={"enabled": on}).raise_for_status()

    def latency(self, ms: int) -> None:
        toxic = {"name": "slow", "type": "latency", "stream": "downstream", "attributes": {"latency": ms}}
        self.api.post(f"/proxies/{self.name}/toxics", json=toxic).raise_for_status()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@pytest.fixture
def proxy(noema_services: Any, noema_run_id: str) -> Iterator[Proxy]:
    api = httpx.Client(base_url=f"http://127.0.0.1:{os.environ.get('NOEMA_TOXIPROXY_PORT', '28474')}", timeout=5.0)
    try:
        api.get("/version").raise_for_status()
    except httpx.HTTPError:
        api.close()
        pytest.skip("the host toxiproxy is not running: make up")
    upstream = urlsplit(noema_services.redis_url)
    name, port = f"{noema_run_id}-adm-valkey", _free_port()
    body = {"name": name, "listen": f"127.0.0.1:{port}", "upstream": f"{upstream.hostname}:{upstream.port}"}
    api.post("/proxies", json={**body, "enabled": True}).raise_for_status()
    try:
        yield Proxy(api, name, f"redis://127.0.0.1:{port}/0")
    finally:
        api.delete(f"/proxies/{name}")
        api.close()


@pytest.fixture
async def via_proxy(
    proxy: Proxy, noema_services: Any, noema_run_id: str
) -> AsyncIterator[tuple[redis.asyncio.Redis, redis.asyncio.Redis, RedisKeys]]:
    """(a client through the proxy, a direct client, the namespace)."""
    async with redis_namespace(noema_services.redis_url, noema_run_id) as (direct, keys):
        client = redis.asyncio.Redis.from_url(proxy.url, decode_responses=False, socket_connect_timeout=0.2)
        try:
            yield client, direct, keys
        finally:
            await client.aclose()


def degraded(adm: GcraAdmission) -> bool:
    return adm.degraded  # a call, so mypy does not narrow the property across the cut


def graph(calls: int = 1, streams: int = 0) -> WorkGraph:
    return WorkGraph({("sol-6.1", "generation"): calls}, {"sol-6.1": 10}, calls, 5, 5, streams)


def registry(**limits: Any) -> FakeConfigRegistry:
    return FakeConfigRegistry(snapshot_from(patched_example({"tenants": {"t_acme": {"limits": limits}}})))


async def test_tadm2_redis_down_admits_at_the_local_share_then_recovers(proxy: Proxy, via_proxy: Any) -> None:
    client, direct, keys = via_proxy
    clock = FakeClock()
    adm = GcraAdmission(
        clock, registry(rpm_per_key=40), keys, client, env="test", expected_pods=PODS, redis_timeout_s=0.5
    )
    await adm.reserve(principal(), graph(), queue_timeout_s=0)
    assert not degraded(adm) and await direct.exists(keys.gcra("key:key_1:rpm"))
    proxy.enabled(False)
    for _ in range(40 // PODS):  # the local share: 10 calls per minute on this pod
        await adm.reserve(principal(), graph(), queue_timeout_s=0)
    assert degraded(adm)
    with pytest.raises(NoemaError) as err:
        await adm.reserve(principal(), graph(), queue_timeout_s=0)
    assert err.value.code is ErrorCode.RPM_EXCEEDED
    proxy.enabled(True)
    await clock.advance(1.0)  # the next probe
    res = await adm.reserve(principal(), graph(), queue_timeout_s=0)
    assert not degraded(adm)
    tat = float(await direct.get(keys.gcra("key:key_1:rpm")))
    assert tat - clock.now().timestamp() * 1000 == pytest.approx(2 * 1500 - 1000, abs=1)  # 2 calls on Redis, 1 s on
    assert res.headers()["x-ratelimit-limit-requests"] == "40"  # the full limit again: local state dropped


async def test_tadm2_unreachable_redis_admits_at_the_local_share(noema_run_id: str) -> None:
    """The down case without the host toxiproxy: nothing listens on the port (connection refused)."""
    client = redis.asyncio.Redis.from_url(f"redis://127.0.0.1:{_free_port()}/0", socket_connect_timeout=0.2)
    keys = RedisKeys(f"ns:{noema_run_id}:adm-closed:")
    telemetry = FakeTelemetry()
    adm = GcraAdmission(
        FakeClock(),
        registry(rpm_per_key=40),
        keys,
        client,
        env="test",
        instruments=telemetry.instruments,
        expected_pods=PODS,
        redis_timeout_s=0.5,
    )
    try:
        for _ in range(40 // PODS):
            res = await adm.reserve(principal(), graph(), queue_timeout_s=0)
            assert res.headers()["x-ratelimit-limit-requests"] == str(40 // PODS)
        assert degraded(adm)
        with pytest.raises(NoemaError) as err:
            await adm.reserve(principal(), graph(), queue_timeout_s=0)
        assert err.value.code is ErrorCode.RPM_EXCEEDED and err.value.retry_after_s == 6  # T = 6 s per pod
        await res.release()  # a local debit: refunded locally, no Redis call
        assert [m.value for m in telemetry.metrics if m.name == "aghoo_admission_degraded"] == [0, 1]
        assert telemetry.counter("aghoo_admission_rejections_total", code="rpm_exceeded") == 1
        assert telemetry.counter("aghoo_admission_refund_failures_total") == 0
    finally:
        await client.aclose()


async def test_slot_releases_lost_in_an_outage_are_retried_after_it(proxy: Proxy, via_proxy: Any) -> None:
    client, direct, keys = via_proxy
    config = registry(concurrent_streams_per_project=8)
    telemetry = FakeTelemetry()
    adm = GcraAdmission(
        FakeClock(),
        config,
        keys,
        client,
        env="test",
        instruments=telemetry.instruments,
        expected_pods=PODS,
        redis_timeout_s=0.5,
    )
    held = [await adm.reserve(principal(project_id=p), graph(streams=1), queue_timeout_s=0) for p in ("p1", "p2")]
    proxy.enabled(False)
    await held[0].release()  # the Redis refund fails: its slot release is kept
    assert adm._store.pending_releases == 1 and not degraded(adm)
    proxy.enabled(True)
    await held[1].release()  # carries the kept release
    assert adm._store.pending_releases == 0
    assert [await direct.zcard(keys.streams("t_acme", p)) for p in ("p1", "p2")] == [0, 0]
    assert telemetry.counter("aghoo_admission_refund_failures_total") == 1
    assert [m.value for m in telemetry.metrics if m.name == "aghoo_admission_pending_releases"] == [0, 1, 0]


async def test_tadm2_stream_slots_are_divided_too(proxy: Proxy, via_proxy: Any) -> None:
    client, _direct, keys = via_proxy
    clock = FakeClock()
    config = registry(concurrent_streams_per_project=8)
    adm = GcraAdmission(clock, config, keys, client, env="test", expected_pods=PODS, redis_timeout_s=0.5)
    proxy.enabled(False)
    held = [await adm.reserve(principal(), graph(streams=1), queue_timeout_s=0) for _ in range(8 // PODS)]
    with pytest.raises(NoemaError) as err:
        await adm.reserve(principal(), graph(streams=1), queue_timeout_s=0)
    assert err.value.code is ErrorCode.QUEUE_TIMEOUT
    await held[0].release()
    await adm.reserve(principal(), graph(streams=1), queue_timeout_s=0)


async def test_a_reply_slower_than_50_ms_counts_as_down(proxy: Proxy, via_proxy: Any) -> None:
    client, _direct, keys = via_proxy
    adm = GcraAdmission(FakeClock(), registry(), keys, client, env="test", expected_pods=PODS)  # 50 ms default
    proxy.latency(300)
    res = await adm.reserve(principal(), graph(), queue_timeout_s=0)
    assert degraded(adm) and res.headers()["x-ratelimit-limit-requests"] == str(6000 // PODS)
    await res.release()  # a Redis-down refund of a local debit: no error, no wait


async def test_rate_limiter_never_raises_when_redis_is_down(proxy: Proxy, via_proxy: Any) -> None:
    client, _direct, keys = via_proxy
    clock = FakeClock()
    limiter = GcraRateLimiter(clock, keys, client, expected_pods=5, redis_timeout_s=0.5)
    assert await limiter.debit("authfail:198.51.100.9", rate_per_s=10, burst=50) is None
    proxy.enabled(False)
    for _ in range(50 // 5):  # burst 10 per pod
        assert await limiter.debit("authfail:198.51.100.9", rate_per_s=10, burst=50) is None
    retry = await limiter.debit("authfail:198.51.100.9", rate_per_s=10, burst=50)
    assert retry is not None and retry == pytest.approx(0.5)  # 2/s per pod
    assert await limiter.check("authfail:198.51.100.9", rate_per_s=10, burst=50) is not None


async def test_a_reserve_whose_reply_came_too_late_is_compensated_on_redis(
    noema_services: Any, noema_run_id: str
) -> None:
    """Wave E edge fix 1: the RESERVE script ran on Valkey but its reply came after the 50 ms bound. The pod
    admits on its local share, and the Redis-side debit is taken back at once (bucket units and slot members), so
    no phantom stream slot fills the project's set after the probe."""
    async with redis_namespace(noema_services.redis_url, noema_run_id) as (client, keys):
        clock = FakeClock()
        adm = GcraAdmission(
            clock, registry(concurrent_streams_per_project=1), keys, client, env="test", expected_pods=1
        )
        primary = adm._store._redis
        assert primary is not None
        script = primary._reserve

        async def late_reply(**kw: Any) -> Any:
            out = await script(**kw)  # ran atomically on Valkey
            await anyio.sleep(0.2)  # the reply is "late" (a loop stall or a slow network)
            return out

        primary._reserve = late_reply  # type: ignore[assignment]
        res = await adm.reserve(principal(), graph(streams=1), queue_timeout_s=0)
        assert degraded(adm)
        assert await client.zcard(keys.streams("t_acme", "p1")) == 0  # the slot member was removed again
        assert await client.get(keys.gcra("key:key_1:rpm")) is None  # and the calls refunded
        await res.release()
        primary._reserve = script
        await clock.advance(1.0)  # the next probe uses Redis again
        again = await adm.reserve(principal(), graph(streams=1), queue_timeout_s=0)
        assert not degraded(adm)
        await again.release()
        assert await client.zcard(keys.streams("t_acme", "p1")) == 0
