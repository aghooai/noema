"""`FakeWorld`: every fake wired together on one `FakeClock`, one config registry, one scenario and one store.

`build_world()` is what `noema.composition.build(env, fakes=…)` uses for the fields it fakes, and what C-wave tests
use directly (`world.decisions`, `world.adapters`, `world.harness.release("barrier")`, `world.clock.autojump()`).
`world.runtime` is a real `TaskGroupRuntime` on the world clock (open it with `await world.runtime.open()` and close
it in the same task).
`world.fake(name)` returns the fake for a `Services` field name (`FAKE_FIELDS`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Final

from noema.core.clock import Clock
from noema.core.protocols.telemetry import Telemetry
from noema.core.redis_keys import RedisKeys
from noema.core.runtime import TaskGroupRuntime

from .adapters import FakeAdapter, FakeAdapterRegistry
from .auth import FakeAuthenticator, FakeAuthorizer
from .clock import FakeClock, FakeIdGen
from .config import FakeConfigRegistry
from .credentials import FakeBroker
from .db import FakeDatabase
from .db_system import FakeSystemCalls
from .decision import FakeDecisionCache, FakeQuestionCompiler
from .decision_engine import FakeDecisionEngine, scenario_planner
from .events import FakeEventMirror, FakeSseWriter, FakeTurnEventLog
from .generation import FakeGenerationEngine
from .health import FakeHealthRegistry, FakeRetryBudget
from .idempotency import FakeIdempotencyStore
from .jobs import FakeJobQueue
from .labeling import FakeCalibratorRegistry, FakeLabelingSampler
from .media import FakeFileService
from .media_views import FakeInputViewResolver, FakeMediaBudgetPlanner, FakeMediaNegotiator
from .ops_admission import FakeAdmission
from .ops_planning import FakeTokenEstimator, FakeWorkGraphPlanner
from .outbox import FakeCostMeter, FakeOutbox
from .s3 import FakeS3Client
from .scenario import Scenario
from .scenario_harness import ScenarioHarness
from .secrets import FakeSecretsProvider
from .telemetry import FakeTelemetry

EMPTY_SCENARIO: Final[Mapping[str, Any]] = {"scenario": "empty", "rules": []}
PLATFORM_SECRETS: Final[Mapping[str, str]] = {
    "noema/api-key-pepper": "fake-api-key-pepper-0123456789abcdef",
    "noema/input-digest-pepper": "fake-input-digest-pepper-0123456789ab",
}


@dataclass
class FakeWorld:
    """Every fake, named like the `Services` fields they stand in for (plus the test handles)."""

    clock: Clock
    ids: FakeIdGen
    config: FakeConfigRegistry
    harness: ScenarioHarness
    secrets: FakeSecretsProvider
    broker: FakeBroker
    adapters: FakeAdapterRegistry
    health: FakeHealthRegistry
    retry_budget: FakeRetryBudget
    planner: FakeWorkGraphPlanner
    admission: FakeAdmission
    estimator: FakeTokenEstimator
    cost: FakeCostMeter
    outbox: FakeOutbox
    db: FakeDatabase
    jobs: FakeJobQueue
    idempotency: FakeIdempotencyStore
    events: FakeTurnEventLog
    sse: FakeSseWriter
    files: FakeFileService
    views: FakeInputViewResolver
    negotiator: FakeMediaNegotiator
    media_budget: FakeMediaBudgetPlanner
    calibrators: FakeCalibratorRegistry
    sampler: FakeLabelingSampler
    compiler: FakeQuestionCompiler
    decision_cache: FakeDecisionCache
    decisions: FakeDecisionEngine
    generation: FakeGenerationEngine
    authn: FakeAuthenticator
    authz: FakeAuthorizer
    telemetry: Telemetry
    keys: RedisKeys
    s3: FakeS3Client
    runtime: TaskGroupRuntime
    mirror: FakeEventMirror

    @property
    def fake_clock(self) -> FakeClock:
        """The clock as a `FakeClock` (AssertionError when the world was built on another clock)."""
        assert isinstance(self.clock, FakeClock), "this world runs on a real clock"
        return self.clock

    def use_scenario(self, scenario: Scenario | str | Path | Mapping[str, Any]) -> ScenarioHarness:
        """Load a scenario DSL document into every scenario-driven fake (adapters, decision engine); returns the
        new harness (barriers: `harness.release(name)`)."""
        harness = ScenarioHarness(scenario, self.clock)
        self.harness = harness
        for adapter in self.adapters.adapters.values():
            adapter.harness = harness
        self.decisions.harness = harness
        self.decisions.planner = scenario_planner(harness)
        return harness

    def fake(self, name: str) -> object:
        """The fake standing in for the `Services` field `name` (KeyError for fields without a fake)."""
        if name not in FAKE_FIELDS:
            raise KeyError(f"no fake for Services.{name}")
        return getattr(self, name)


_HANDLES: Final = frozenset({"harness", "mirror"})
FAKE_FIELDS: Final[frozenset[str]] = frozenset(f.name for f in fields(FakeWorld)) - _HANDLES


def build_world(
    *,
    clock: Clock | None = None,
    telemetry: Telemetry | None = None,
    config: FakeConfigRegistry | None = None,
    scenario: Scenario | str | Path | Mapping[str, Any] | None = None,
    env: str = "test",
    namespace: str = "ns:fake:",
    hold_decisions_until_cancel: bool = False,
) -> FakeWorld:
    """Wire every fake on one clock and config (default: the shipped example config and an empty scenario)."""
    clock = clock or FakeClock()
    ids = FakeIdGen(clock)
    config = config or FakeConfigRegistry()
    harness = ScenarioHarness(scenario if scenario is not None else EMPTY_SCENARIO, clock)
    telemetry = telemetry if telemetry is not None else FakeTelemetry(strict=env == "test")
    db = FakeDatabase(clock)
    outbox = FakeOutbox(sink=FakeSystemCalls(db.store).outbox_apply)
    broker = FakeBroker(clock, config, mantle_mode=config.current().platform.credentials.mantle_auth)
    health = FakeHealthRegistry(clock)
    retry_budget = FakeRetryBudget(clock)
    adapters = FakeAdapterRegistry(
        config,
        {
            kind: FakeAdapter(
                kind, clock=clock, config=config, harness=harness, broker=broker, retry_budget=retry_budget
            )
            for kind in ("bedrock-converse", "bedrock-mantle-chat")
        },
    )
    cost = FakeCostMeter(clock, config, outbox, instruments=telemetry.instruments)
    sampler = FakeLabelingSampler(clock, config, outbox)
    files = FakeFileService(clock, ids)
    mirror = FakeEventMirror(clock)
    keys = RedisKeys(namespace)
    return FakeWorld(
        clock=clock,
        ids=ids,
        config=config,
        harness=harness,
        secrets=FakeSecretsProvider(clock, PLATFORM_SECRETS),
        broker=broker,
        adapters=adapters,
        health=health,
        retry_budget=retry_budget,
        planner=FakeWorkGraphPlanner(),
        admission=FakeAdmission(clock, config, env=env, instruments=telemetry.instruments),
        estimator=FakeTokenEstimator(),
        cost=cost,
        outbox=outbox,
        db=db,
        jobs=FakeJobQueue(clock, db, ids),
        idempotency=FakeIdempotencyStore(clock, ids, ttl_s=config.current().platform.idempotency.ttl_s),
        events=FakeTurnEventLog(clock, mirror),
        sse=FakeSseWriter(clock),
        files=files,
        views=FakeInputViewResolver(files),
        negotiator=FakeMediaNegotiator(files),
        media_budget=FakeMediaBudgetPlanner(),
        calibrators=FakeCalibratorRegistry(config),
        sampler=sampler,
        compiler=FakeQuestionCompiler(config),
        decision_cache=FakeDecisionCache(clock, keys),
        decisions=FakeDecisionEngine(
            clock,
            ids,
            harness=harness,
            cost=cost,
            db=db,
            sampler=sampler,
            outbox=outbox,
            hold_until_cancel=hold_decisions_until_cancel,
        ),
        generation=FakeGenerationEngine(clock, ids, adapters, health=health, retry_budget=retry_budget, cost=cost),
        authn=FakeAuthenticator(),
        authz=FakeAuthorizer(),
        telemetry=telemetry,
        keys=keys,
        s3=FakeS3Client(),
        runtime=TaskGroupRuntime(clock),
        mirror=mirror,
    )
