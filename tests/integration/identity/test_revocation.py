"""SEC-AUTH-02 (Redis up): a revocation reaches every pod's key cache within 1 s through `keys_revoked`; a forged
cross-tenant message is dropped (SEC-ISO-02, identity's channel)."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
import orjson
import pytest
import redis.asyncio
from tests.fakes.clock import FakeClock
from tests.fakes.db import FakeDatabase
from tests.fakes.ops_admission import FakeRateLimiter
from tests.fakes.telemetry import FakeTelemetry
from tests.unit.identity.helpers import KEY_ID, TENANT, bearer, pepper_secrets, seed_key

from noema.core.clock import SystemClock
from noema.core.errors import ErrorCode, NoemaError
from noema.core.redis_keys import RedisKeys
from noema.core.runtime import TaskGroupRuntime
from noema.identity.authn import ApiKeyAuthenticator
from noema.identity.revocation import run_revocation_listener
from noema.identity.security import SecurityEvents

pytestmark = pytest.mark.integration


def pod(clock: FakeClock, db: FakeDatabase) -> ApiKeyAuthenticator:
    return ApiKeyAuthenticator(
        db=db,
        secrets=pepper_secrets(clock),
        limiter=FakeRateLimiter(clock),
        clock=clock,
        runtime=TaskGroupRuntime(clock),
        env="test",
    )


async def until(probe: Callable[[], Awaitable[int]], value: int) -> None:
    """Poll `probe` every 10 ms until it returns `value` (the caller bounds the wait)."""
    while await probe() != value:  # noqa: ASYNC110 - polling external state (Redis, the pods' caches)
        await anyio.sleep(0.01)


async def _subscribers(client: redis.asyncio.Redis, channel: str) -> int:
    return int((await client.pubsub_numsub(channel))[0][1])


async def _counted(telemetry: FakeTelemetry) -> int:
    return int(telemetry.counter("aghoo_security_violations_total", kind="pubsub_tenant_mismatch"))


async def _cached(pods: list[ApiKeyAuthenticator]) -> int:
    return sum(len(a.cache) for a in pods)


async def test_revocation_reaches_every_pod_within_a_second(noema_services: Any, noema_run_id: str) -> None:
    clock = FakeClock()
    db = FakeDatabase(clock)
    token = seed_key(db)
    pods = [pod(clock, db), pod(clock, db)]
    for authn in pods:
        await authn.authenticate(bearer(token), "198.51.100.1")
        assert len(authn.cache) == 1
    keys = RedisKeys(f"ns:{noema_run_id}:rev:")
    client = redis.asyncio.Redis.from_url(noema_services.redis_url, decode_responses=False)
    telemetry = FakeTelemetry(strict=True)
    security = SecurityEvents(telemetry=telemetry)
    try:
        async with anyio.create_task_group() as tg:
            for authn in pods:
                tg.start_soon(run_revocation_listener, client, keys, authn, security, SystemClock())
            with anyio.fail_after(5):
                await until(lambda: _subscribers(client, keys.keys_revoked()), 2)
            forged = orjson.dumps({"tenant_id": "t_other", "key_id": KEY_ID})
            await client.publish(keys.keys_revoked(), forged)
            with anyio.fail_after(5):
                await until(lambda: _counted(telemetry), 2)
            assert all(len(a.cache) == 1 for a in pods)  # dropped: the cached row belongs to another tenant
            db.store.table("api_key")[(KEY_ID,)]["revoked_at"] = clock.now()
            started = time.monotonic()
            await client.publish(keys.keys_revoked(), orjson.dumps({"tenant_id": TENANT, "key_id": KEY_ID}))
            with anyio.fail_after(1.0):
                await until(lambda: _cached(pods), 0)
            assert time.monotonic() - started < 1.0
            for authn in pods:
                with pytest.raises(NoemaError) as err:
                    await authn.authenticate(bearer(token), "198.51.100.1")
                assert err.value.code is ErrorCode.INVALID_API_KEY
            tg.cancel_scope.cancel()
    finally:
        await client.aclose()


class _Evictions:
    """The two members `handle_revocation` uses."""

    class cache:  # noqa: N801 - mirrors `ApiKeyAuthenticator.cache`
        @staticmethod
        def peek(key_id: str) -> None:
            return None

    def __init__(self) -> None:
        self.evicted: list[str] = []

    def evict(self, key_id: str) -> bool:
        self.evicted.append(key_id)
        return True


async def test_a_silently_dead_subscription_is_detected_and_resubscribed(
    noema_services: Any, noema_run_id: str
) -> None:
    """Wave E edge fix 22: a pub/sub connection that dies without FIN/RST looks idle forever; the listener PINGs it
    while quiet and resubscribes when no reply comes, so a revocation published after the partition still
    arrives. A healthy quiet connection is kept (its PONGs count)."""
    from urllib.parse import urlsplit

    from tests.integration.db.blackhole import blackhole_proxy

    keys = RedisKeys(f"ns:{noema_run_id}:rev-dead:")
    direct = redis.asyncio.Redis.from_url(noema_services.redis_url, decode_responses=False)
    authn = _Evictions()
    security = SecurityEvents(telemetry=FakeTelemetry(strict=True))
    revoked = keys.keys_revoked()
    try:
        async with blackhole_proxy(int(urlsplit(noema_services.redis_url).port or 6379)) as proxy:
            pod = redis.asyncio.Redis.from_url(f"redis://127.0.0.1:{proxy.port}/0", decode_responses=False)
            async with anyio.create_task_group() as tg:
                tg.start_soon(
                    lambda: run_revocation_listener(
                        pod,
                        keys,
                        authn,  # type: ignore[arg-type]
                        security,
                        SystemClock(),
                        ping_s=0.2,
                        dead_s=0.6,
                    )
                )
                with anyio.fail_after(5):
                    await until(lambda: _subscribers(direct, revoked), 1)
                await anyio.sleep(1.5)  # several PINGs answered: no reconnect
                assert proxy.accepted == 1
                proxy.freeze()  # the partition: the subscribed connection passes nothing from now on
                await direct.publish(revoked, orjson.dumps({"tenant_id": TENANT, "key_id": "k_lost"}))
                with anyio.fail_after(5):
                    await until(lambda: _accepted(proxy), 2)  # resubscribed on a new connection
                    while "k_after" not in authn.evicted:  # published until the new subscription delivers it
                        await direct.publish(revoked, orjson.dumps({"tenant_id": TENANT, "key_id": "k_after"}))
                        await anyio.sleep(0.1)
                assert "k_lost" not in authn.evicted  # sent into the partition: lost (the TTL bounds it)
                tg.cancel_scope.cancel()
            await pod.aclose()
    finally:
        await direct.aclose()


async def _accepted(proxy: Any) -> int:
    return min(int(proxy.accepted), 2)
