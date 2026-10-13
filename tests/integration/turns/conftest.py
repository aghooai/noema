"""Fixtures of the turns integration tests: a namespaced valkey client (keys removed at exit) and a dead one."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from redis.asyncio import Redis

from noema.core.redis_keys import RedisKeys


@pytest.fixture
def keys(noema_run_id: str) -> RedisKeys:
    return RedisKeys(f"ns:{noema_run_id}:{uuid.uuid4().hex[:8]}:")


@pytest.fixture
async def valkey(noema_services: Any, keys: RedisKeys) -> AsyncIterator[Redis]:
    client = Redis.from_url(noema_services.redis_url, decode_responses=False)
    try:
        await client.ping()
    except Exception:  # the compose services are not up
        await client.aclose()
        pytest.skip("valkey is not reachable (make services-up)")
    try:
        yield client
    finally:
        async for key in client.scan_iter(match=f"{keys.ns}*"):
            await client.unlink(key)
        await client.aclose()


@pytest.fixture
async def dead_redis() -> AsyncIterator[Redis]:
    client = Redis.from_url("redis://127.0.0.1:1/0", decode_responses=False, socket_connect_timeout=0.2)
    try:
        yield client
    finally:
        await client.aclose()
