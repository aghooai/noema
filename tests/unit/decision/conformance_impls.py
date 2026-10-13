"""Registrations of the real decision implementations in the shared conformance suites (spec-02 §14): the engine
and the compiler run on the fake world (scenario-driven fake adapters, fake stores) under the suite's FakeClock."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from tests.contract_internal.kits import CacheKit, CompilerKit, DecisionKit
from tests.contract_internal.registry import Harness, register
from tests.fakes.config import FakeConfigRegistry
from tests.fakes.telemetry import FakeTelemetry
from tests.fakes.world import FakeWorld, build_world
from tests.unit.decision.fake_redis import FakeRedis

from noema.core.redis_keys import RedisKeys
from noema.decision.cache import DecisionCacheImpl
from noema.decision.compiler import QuestionCompilerImpl
from noema.decision.deps import EngineDeps
from noema.decision.engine import DecisionEngineImpl


def engine_deps(world: FakeWorld) -> EngineDeps:
    """The real engine's collaborators taken from a fake world."""
    return EngineDeps(
        clock=world.clock,
        ids=world.ids,
        adapters=world.adapters,
        broker=world.broker,
        secrets=world.secrets,
        views=world.views,
        health=world.health,
        estimator=world.estimator,
        cost=world.cost,
        calibrators=world.calibrators,
        sampler=world.sampler,
        cache=world.decision_cache,
        keys=world.keys,
        db=world.db,
        outbox=world.outbox,
        telemetry=world.telemetry,
    )


@register("decision_engine", "real")
@asynccontextmanager
async def real_engine(h: Harness) -> AsyncIterator[DecisionKit]:
    world = build_world(clock=h.clock)

    def use(doc: Mapping[str, Any]) -> None:
        world.use_scenario(doc)

    def db_available(up: bool) -> None:
        world.db.store.available = up

    yield DecisionKit(
        DecisionEngineImpl(engine_deps(world)),
        QuestionCompilerImpl(world.config),
        world.config.current(),
        h.clock,
        use,
        db_available=db_available,
        outbox_records=world.outbox.of_kind,
    )


@register("question_compiler", "real")
@asynccontextmanager
async def real_compiler(h: Harness) -> AsyncIterator[CompilerKit]:
    reg = FakeConfigRegistry()
    yield CompilerKit(QuestionCompilerImpl(reg), reg.current())


@register("decision_cache", "redis-double")
@asynccontextmanager
async def redis_double_cache(h: Harness) -> AsyncIterator[CacheKit]:
    """`DecisionCacheImpl` over the in-memory Redis double (TTL on the suite's clock)."""

    redis = FakeRedis(h.clock)
    keys = RedisKeys("ns:cache:")

    def broken(down: bool) -> None:
        redis.down = down

    yield CacheKit(DecisionCacheImpl(redis, keys, h.clock, FakeTelemetry().instruments), h.clock, keys, broken)  # type: ignore[arg-type]
