"""Registers the real `StreamingGenerationEngine` in the generation_engine conformance suite (spec-02 §14): built
on the suite's FakeClock and the scenario-driven fake adapters, breakers, retry budget, estimator and cost meter of
`tests.fakes.world`."""

from __future__ import annotations

import random
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from tests.contract_internal.kits import GenerationKit
from tests.contract_internal.registry import Harness, register
from tests.fakes.world import FakeWorld, build_world

from noema.generation import StreamingGenerationEngine


def real_engine(world: FakeWorld, *, seed: int = 7) -> StreamingGenerationEngine:
    """The real engine wired to a fake world's services."""
    return StreamingGenerationEngine(
        clock=world.clock,
        ids=world.ids,
        adapters=world.adapters,
        broker=world.broker,
        health=world.health,
        retry_budget=world.retry_budget,
        estimator=world.estimator,
        cost=world.cost,
        telemetry=world.telemetry,
        rng=random.Random(seed),
    )


@register("generation_engine", "streaming")
@asynccontextmanager
async def streaming_generation(h: Harness) -> AsyncIterator[GenerationKit]:
    world = build_world(clock=h.clock)
    snap = world.config.current()

    def use(doc: Mapping[str, Any]) -> None:
        world.use_scenario(doc)

    def force(model_id: str, state: str) -> None:
        world.health.force(model_id, snap.tenant_ref("t_acme").home_region, state)  # type: ignore[arg-type]

    yield GenerationKit(real_engine(world), snap, h.clock, use, force)
