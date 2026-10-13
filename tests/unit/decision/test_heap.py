"""A finished decision leaves no cyclic garbage on the real clock (E3, QL-5): the run drops its coordinator once the
final snapshot is published (the run <-> coordinator callbacks were a cycle), and no deadline scope cancels a
watcher task (a cancelled task's traceback was a cycle). Before E3 one router-default decision left ~670 objects
that only a full collection freed."""

from __future__ import annotations

import dataclasses
import gc
import itertools
from collections import Counter

import pytest
from tests.fakes.world import build_world
from tests.unit.decision.conformance_impls import engine_deps
from tests.unit.decision.test_run import turn_request
from tests.unit.decision.world import EngineWorld

from noema.core.clock import SystemClock
from noema.decision.compiler import QuestionCompilerImpl
from noema.decision.engine import DecisionEngineImpl
from noema.decision.run import DecisionRunImpl

pytestmark = pytest.mark.unit
_inputs = itertools.count()


def _world() -> EngineWorld:
    clock = SystemClock()
    world = build_world(clock=clock)
    rules = [
        {"match": {"prompt_contains": "primary intent"}, "answer": {"dist": {"factual_qa": 0.9, "coding": 0.1}}},
        {"match": {"prompt_contains": "How hard"}, "answer": {"dist": {"moderate": 0.8, "hard": 0.2}}},
        {"match": {}, "answer": {"dist": {"true": 0.05, "false": 0.95}}},
    ]
    world.use_scenario({"scenario": "fast", "seed": 7, "defaults": {"latency": {"fixed_ms": 2}}, "rules": rules})
    engine = DecisionEngineImpl(engine_deps(world))
    return EngineWorld(world, engine, QuestionCompilerImpl(world.config), clock)  # type: ignore[arg-type]


async def _decide(ew: EngineWorld) -> DecisionRunImpl:
    req = dataclasses.replace(turn_request(ew), input=f"What is the capital of France? {next(_inputs)}")  # no L2 hit
    async with ew.engine.run(ew.context(turn_id="turn_1", escalate=False), req) as run:
        assert isinstance(run, DecisionRunImpl)
        s1 = await run.commit_critical()
        assert s1.status in ("quorum", "completed")
        _ = [u async for u in run.updates()]
        final = await run.finalize()
        assert final.final and run.coordinator is None and run.dispatched
        assert run.usage_by_alias()  # the settled usage of the 15 calls
    return run


async def test_a_finished_decision_leaves_no_cyclic_garbage_on_the_real_clock() -> None:
    ew = _world()
    await _decide(ew)  # warm caches
    gc.collect()
    gc.disable()
    gc.set_debug(gc.DEBUG_SAVEALL)
    try:
        for _ in range(3):
            await _decide(ew)
        gc.collect()
        garbage = Counter(f"{type(o).__module__}.{type(o).__qualname__}" for o in gc.garbage)
    finally:
        gc.garbage.clear()
        gc.set_debug(0)
        gc.enable()
    assert sum(garbage.values()) == 0, garbage.most_common(12)
