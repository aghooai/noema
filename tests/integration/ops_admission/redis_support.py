"""Shared Redis/Valkey helpers of the ops-admission tests: a bytes client on the compose Valkey and a fresh
`RedisKeys` namespace per use, unlinked afterwards."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import redis.asyncio

from noema.core.redis_keys import RedisKeys


@asynccontextmanager
async def redis_namespace(redis_url: str, run_id: str) -> AsyncIterator[tuple[redis.asyncio.Redis, RedisKeys]]:
    """A client and a `RedisKeys(f"ns:{run_id}:adm-{8 hex}:")`; every key under it is unlinked on exit."""
    client = redis.asyncio.Redis.from_url(redis_url, decode_responses=False)
    keys = RedisKeys(f"ns:{run_id}:adm-{uuid.uuid4().hex[:8]}:")
    try:
        yield client, keys
    finally:
        try:
            batch = [k async for k in client.scan_iter(match=f"{keys.ns}*", count=500)]
            if batch:
                await client.unlink(*batch)
        finally:
            await client.aclose()
