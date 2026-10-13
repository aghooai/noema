"""`DecisionCacheImpl` on the compose Valkey in the `decision_cache` conformance suite (spec-02 §14). Keys live
under a per-run namespace and are unlinked at the end; TTLs follow the server's clock (virtual_ttl False)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import redis.asyncio
from tests.contract_internal.kits import CacheKit
from tests.contract_internal.registry import Harness, register
from tests.fakes.telemetry import FakeTelemetry

from noema.core.clock import SystemClock
from noema.core.redis_keys import RedisKeys
from noema.decision.cache import DecisionCacheImpl


class _Breakable:
    """Delegates to a real client; `down` makes every call fail like an unreachable server."""

    def __init__(self, client: redis.asyncio.Redis) -> None:
        self._client = client
        self.down = False

    def __getattr__(self, name: str) -> object:
        if self.down:
            raise ConnectionError("valkey unreachable (test switch)")
        return getattr(self._client, name)


@register("decision_cache", "valkey", marks=(pytest.mark.integration,))
@asynccontextmanager
async def valkey_cache(h: Harness) -> AsyncIterator[CacheKit]:
    url = h.fixture("noema_services").redis_url
    client = redis.asyncio.Redis.from_url(url, decode_responses=False)
    keys = RedisKeys(f"ns:{h.run_id}:dec:")
    proxy = _Breakable(client)

    def broken(down: bool) -> None:
        proxy.down = down

    cache = DecisionCacheImpl(proxy, keys, SystemClock(), FakeTelemetry().instruments)  # type: ignore[arg-type]
    try:
        yield CacheKit(cache, h.clock, keys, broken, virtual_ttl=False)
    finally:
        async for key in client.scan_iter(match=f"ns:{h.run_id}:dec:*"):
            await client.unlink(key)
        await client.aclose()
