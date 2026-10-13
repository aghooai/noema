"""Test rig of the generation unit tests: the real engine on a FakeClock with a scripted adapter and the fake
breakers, retry budget, estimator, cost meter and telemetry of `tests.fakes.world`."""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import anyio
from tests.contract_internal.kits import principal
from tests.fakes.clock import FakeClock
from tests.fakes.generation import StaticPromptBuilder
from tests.fakes.ops_admission import CountingReservation
from tests.fakes.telemetry import FakeTelemetry
from tests.fakes.world import FakeWorld, build_world

from noema.core.clock import Deadline
from noema.core.protocols.config import ConfigSnapshot
from noema.core.protocols.generation import AttemptBudget, AttemptRecord, AttemptSpec, PromptBuilder, TurnContext
from noema.core.protocols.ops import CostMeter
from noema.generation import StreamingGenerationEngine, StreamingProfileRun

from .scripted import Call, ScriptedAdapter, ScriptedRegistry

SOL, OPUS, GLM = "us.openai.gpt-6.1-sol", "us.anthropic.claude-opus-5-5", "us.zai.glm-5.3"
Action = tuple[float, Callable[[StreamingProfileRun], Awaitable[None]]]


@dataclass
class Rig:
    clock: FakeClock
    world: FakeWorld
    adapter: ScriptedAdapter
    engine: StreamingGenerationEngine
    reservation: CountingReservation = field(default_factory=CountingReservation)

    @property
    def snap(self) -> ConfigSnapshot:
        return self.world.config.current()

    @property
    def telemetry(self) -> FakeTelemetry:
        telemetry = self.world.telemetry
        assert isinstance(telemetry, FakeTelemetry)
        return telemetry

    def ctx(
        self, *, deadline_s: float = 120.0, session_id: str | None = "ses_1", snapshot: ConfigSnapshot | None = None
    ) -> TurnContext:
        snap = snapshot or self.snap
        return TurnContext(
            principal(),
            snap.tenant_ref("t_acme"),
            snap,
            Deadline(self.clock.monotonic() + deadline_s, self.clock),
            self.reservation,
            "turn_1",
            session_id,
            "0" * 32,
        )


def make_rig(scripts: dict[str, Sequence[Call]], *, seed: int = 7, cost: CostMeter | None = None) -> Rig:
    clock = FakeClock()
    world = build_world(clock=clock)
    adapter = ScriptedAdapter(clock, world.config.current(), {k: list(v) for k, v in scripts.items()})
    engine = StreamingGenerationEngine(
        clock=clock,
        ids=world.ids,
        adapters=ScriptedRegistry(adapter),
        broker=world.broker,
        health=world.health,
        retry_budget=world.retry_budget,
        estimator=world.estimator,
        cost=cost or world.cost,
        telemetry=world.telemetry,
        rng=random.Random(seed),
    )
    return Rig(clock, world, adapter, engine)


@dataclass
class Outcome:
    events: list[tuple[int, dict[str, Any]]]
    records: tuple[AttemptRecord, ...]
    first_ms: int | None
    run: StreamingProfileRun

    def types(self) -> list[str]:
        return [ev["type"] for _, ev in self.events]

    def of(self, kind: str) -> list[tuple[int, dict[str, Any]]]:
        return [(ms, ev) for ms, ev in self.events if ev["type"] == kind]

    def text(self) -> str:
        return "".join(ev["text"] for _, ev in self.of("generation.delta"))


async def drive(
    rig: Rig,
    profile: str = "sol-fast",
    *,
    effort: Any = None,
    max_output_tokens: int | None = None,
    hold: bool = True,
    speculative: bool = True,
    auto: Literal["spec_agrees", "gate"] = "spec_agrees",
    budget: AttemptBudget | None = None,
    actions: Sequence[Action] = (),
    builder: PromptBuilder | None = None,
    ctx: TurnContext | None = None,
) -> Outcome:
    """Run one profile under autojump; `actions` run at their offsets (seconds from the start)."""
    clock = rig.clock
    t0 = clock.monotonic()
    events: list[tuple[int, dict[str, Any]]] = []
    first: list[float | None] = []

    def ms() -> int:
        return round((clock.monotonic() - t0) * 1000)

    async with clock.autojump():
        cm = rig.engine.run(
            ctx or rig.ctx(),
            AttemptSpec(profile, effort, max_output_tokens, "none"),
            builder or StaticPromptBuilder(),
            hold=hold,
            speculative=speculative,
            auto_commit=auto,
            failure_budget=budget or AttemptBudget(),
        )
        async with cm as run, anyio.create_task_group() as tg:

            async def read() -> None:
                events.extend([(ms(), dict(ev)) async for ev in run.events()])

            async def control() -> None:
                ft = await run.first_text()
                first.append(None if ft is None else ft - t0)

            tg.start_soon(read)
            tg.start_soon(control)
            for at, action in sorted(actions, key=lambda a: a[0]):
                await clock.sleep(max(0.0, t0 + at - clock.monotonic()))
                await action(run)
            records = await run.result()
    first_ms = None if not first or first[0] is None else round(first[0] * 1000)
    return Outcome(events, records, first_ms, run)


def commit(reason: Literal["spec_agrees", "holdback_deadline", "gate"] = "spec_agrees") -> Callable[..., Any]:
    async def act(run: StreamingProfileRun) -> None:
        await run.commit(reason)

    return act


def cancel(reason: Literal["spec_changed", "client_abort", "deadline"]) -> Callable[..., Any]:
    async def act(run: StreamingProfileRun) -> None:
        await run.cancel(reason)

    return act
