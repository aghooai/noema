"""Registrations of the real ops-admission implementations in the conformance suites (spec-02 §14):
`DefaultWorkGraphPlanner` (work_graph), `GcraAdmission` and `GcraRateLimiter` on the in-process store ("local")
and on the compose Valkey ("redis", integration)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from tests.contract_internal.kits import AdmissionKit, PlannerKit, RateLimiterKit
from tests.contract_internal.registry import Harness, register
from tests.fakes.config import example_snapshot

from noema.core.protocols.config import ConfigRegistry
from noema.core.redis_keys import RedisKeys
from noema.ops.admission import DefaultWorkGraphPlanner, GcraAdmission, GcraRateLimiter

from .redis_support import redis_namespace

TEST_REDIS_TIMEOUT_S = 2.0  # generous under xdist: these suites test semantics, not the 50 ms failover bound


@register("work_graph", "default")
@asynccontextmanager
async def planner(h: Harness) -> AsyncIterator[PlannerKit]:
    yield PlannerKit(DefaultWorkGraphPlanner(), example_snapshot())


@register("admission", "gcra-local")
@asynccontextmanager
async def local_admission(h: Harness) -> AsyncIterator[AdmissionKit]:
    keys = RedisKeys(f"ns:{h.run_id}:local:")
    yield AdmissionKit(lambda config: GcraAdmission(h.clock, config, keys, None, env="test"), h.clock)


@register("admission", "gcra-redis", marks=(pytest.mark.integration,))
@asynccontextmanager
async def redis_admission(h: Harness) -> AsyncIterator[AdmissionKit]:
    async with redis_namespace(h.fixture("noema_services").redis_url, h.run_id) as (client, keys):

        def make(config: ConfigRegistry) -> GcraAdmission:
            return GcraAdmission(h.clock, config, keys, client, env="test", redis_timeout_s=TEST_REDIS_TIMEOUT_S)

        yield AdmissionKit(make, h.clock)


@register("rate_limiter", "gcra-local")
@asynccontextmanager
async def local_rate_limiter(h: Harness) -> AsyncIterator[RateLimiterKit]:
    yield RateLimiterKit(GcraRateLimiter(h.clock, RedisKeys(f"ns:{h.run_id}:local:"), None), h.clock)


@register("rate_limiter", "gcra-redis", marks=(pytest.mark.integration,))
@asynccontextmanager
async def redis_rate_limiter(h: Harness) -> AsyncIterator[RateLimiterKit]:
    async with redis_namespace(h.fixture("noema_services").redis_url, h.run_id) as (client, keys):
        limiter = GcraRateLimiter(h.clock, keys, client, redis_timeout_s=TEST_REDIS_TIMEOUT_S)
        yield RateLimiterKit(limiter, h.clock)
