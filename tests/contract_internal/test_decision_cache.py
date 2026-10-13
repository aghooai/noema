"""DecisionCache conformance (spec-02 §5, IMPL-004): round trip, TTL, tenant purge via `RedisKeys.l2_pattern`,
and fail-open (errors and timeouts -> miss / no-op, never raised)."""

from __future__ import annotations

import pytest

from .kits import CacheKit
from .registry import kit_fixture

kit = kit_fixture("decision_cache")


async def test_round_trip_and_miss(kit: CacheKit) -> None:
    key = kit.keys.l2("t_acme", "abc")
    assert await kit.cache.get(key) is None
    await kit.cache.put(key, b"artifact", 60)
    assert await kit.cache.get(key) == b"artifact"


async def test_ttl_expires(kit: CacheKit) -> None:
    if not kit.virtual_ttl:
        pytest.skip("TTL follows a real clock")
    key = kit.keys.l2("t_acme", "ttl")
    await kit.cache.put(key, b"x", 10)
    await kit.clock.advance(9)
    assert await kit.cache.get(key) == b"x"
    await kit.clock.advance(2)
    assert await kit.cache.get(key) is None


async def test_purge_tenant_only_touches_that_tenant(kit: CacheKit) -> None:
    for tenant in ("t_acme", "t_acme", "t_other"):
        for digest in ("d1", "d2"):
            await kit.cache.put(kit.keys.l2(tenant, digest), b"x", 60)
    assert await kit.cache.purge_tenant("t_acme") == 2
    assert await kit.cache.get(kit.keys.l2("t_acme", "d1")) is None
    assert await kit.cache.get(kit.keys.l2("t_other", "d1")) == b"x"


async def test_fail_open(kit: CacheKit) -> None:
    if kit.break_backend is None:
        pytest.skip("no failure hook")
    key = kit.keys.l2("t_acme", "down")
    await kit.cache.put(key, b"x", 60)
    kit.break_backend(True)
    assert await kit.cache.get(key) is None
    await kit.cache.put(key, b"y", 60)  # swallowed
    kit.break_backend(False)
    assert await kit.cache.get(key) == b"x"
