"""The real decision engine on the fake world (scenario-driven fake adapters, FakeClock) for engine-level tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, cast

from tests.contract_internal.kits import NullReservation, principal
from tests.fakes.clock import FakeClock
from tests.fakes.config import FakeConfigRegistry, patched_example, snapshot_from
from tests.fakes.telemetry import FakeTelemetry
from tests.fakes.world import FakeWorld, build_world
from tests.unit.decision.conformance_impls import engine_deps

from noema.core.clock import Deadline
from noema.core.protocols.config import ConfigSnapshot
from noema.core.protocols.decision import (
    DecisionContext,
    DecisionInput,
    DecisionRequest,
    DecisionSnapshot,
    OutputFormat,
    ProfileRef,
    Purpose,
)
from noema.core.protocols.ops import CostMeter
from noema.core.values import ContentPolicy, DataClass
from noema.decision.compiler import QuestionCompilerImpl
from noema.decision.engine import DecisionEngineImpl

HAIKU = "us.anthropic.claude-haiku-5-5"
LUNA = "us.openai.gpt-6-luna"
DEEPSEEK = "deepseek.v3.2"
TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"


@dataclass
class EngineWorld:
    world: FakeWorld
    engine: DecisionEngineImpl
    compiler: QuestionCompilerImpl
    clock: FakeClock

    @property
    def snapshot(self) -> ConfigSnapshot:
        return self.world.config.current()

    def use(self, rules: Sequence[Mapping[str, Any]], *, name: str = "t") -> None:
        self.world.use_scenario({"scenario": name, "seed": 7, "defaults": {"latency": {"fixed_ms": 0}}, "rules": rules})

    def request(
        self,
        questions: Sequence[Mapping[str, Any]],
        *,
        profile: str = "router-default",
        value: DecisionInput = "My package says delivered but I was also charged twice.",
        deadline_ms: int | None = None,
        min_quorum: int | None = None,
        escalation: str = "off",
        output_format: OutputFormat = "native",
        include: tuple[str, ...] = ("members",),
    ) -> DecisionRequest:
        p = self.snapshot.decision_profile(profile)
        qs = self.compiler.compile(p, questions, origin="native", output_format=output_format)
        return DecisionRequest(
            ProfileRef(p.id, p.version),
            qs,
            value,
            deadline_ms or p.deadline_ms,
            min_quorum or p.min_quorum,
            "off" if escalation == "off" else "profile_default",
            frozenset(include),  # type: ignore[arg-type]
            "native",
            output_format,
        )

    def context(
        self,
        *,
        budget_s: float = 120.0,
        escalate: bool = True,
        turn_id: str | None = None,
        store: bool = True,
        purpose: Purpose = "decision",
        data_class: DataClass = "internal",
        content_policy: ContentPolicy = "full",
        tenant: str = "t_acme",
        escalate_until_s: float | None = None,
    ) -> DecisionContext:
        deadline = Deadline(self.clock.monotonic() + budget_s, self.clock)
        until = (
            deadline if escalate_until_s is None else Deadline(self.clock.monotonic() + escalate_until_s, self.clock)
        )
        who = replace(principal(), tenant_id=tenant)
        return DecisionContext(
            principal=who,
            tenant=self.snapshot.tenant_ref(tenant),
            snapshot=self.snapshot,
            deadline=deadline,
            escalate_until=until if escalate else None,
            reservation=NullReservation(),
            purpose=purpose,
            store=store,
            data_class=data_class,
            content_policy=content_policy,
            trace_id=TRACE,
            turn_id=turn_id,
            session_id="ses_1" if turn_id else None,
        )


def engine_world(*, patch: Mapping[str, Any] | None = None, cost: CostMeter | None = None) -> EngineWorld:
    """A fresh world (the example config, optionally deep-merged with `patch`) with the real engine on it."""
    clock = FakeClock()
    config = FakeConfigRegistry(snapshot_from(patched_example(patch))) if patch else None
    world = build_world(clock=clock, config=config)
    deps = engine_deps(world)
    if cost is not None:
        deps = replace(deps, cost=cost)
    return EngineWorld(world, DecisionEngineImpl(deps), QuestionCompilerImpl(world.config), clock)


def answer(
    model: str | None, contains: str | None, dist: Mapping[str, float], ms: float, **extra: Any
) -> dict[str, Any]:
    """One scenario rule answering `dist` after `ms`."""
    match: dict[str, Any] = {}
    if model:
        match["model"] = model
    if contains:
        match["prompt_contains"] = contains
    rule: dict[str, Any] = {"match": match, "answer": {"dist": dict(dist), **extra.pop("answer", {})}, "latency_ms": ms}
    rule.update(extra)
    return rule


def fault(model: str | None, kind: str, ms: float = 10, contains: str | None = None, **extra: Any) -> dict[str, Any]:
    match: dict[str, Any] = {}
    if model:
        match["model"] = model
    if contains:
        match["prompt_contains"] = contains
    return {"match": match, "fault": {"kind": kind, **extra}, "latency_ms": ms}


def res(snapshot: DecisionSnapshot) -> Any:
    """A snapshot's result for test indexing (JSON values are untyped there)."""
    return snapshot.result


def tel(ew: EngineWorld) -> FakeTelemetry:
    """The world's telemetry as the recording fake."""
    return cast(FakeTelemetry, ew.world.telemetry)
