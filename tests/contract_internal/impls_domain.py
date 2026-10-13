"""Registrations: the fakes of the domain suites (adapters, decision, generation, streaming, media)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from tests.fakes.config import FakeConfigRegistry, example_snapshot
from tests.fakes.decision import FakeDecisionCache, FakeQuestionCompiler
from tests.fakes.events import FakeEventMirror, FakeSseWriter, FakeTurnEventLog
from tests.fakes.media_views import FakeSandboxRunner
from tests.fakes.world import FakeWorld, build_world

from .kits import (
    AdapterKit,
    CacheKit,
    CompilerKit,
    DecisionKit,
    EventLogKit,
    GenerationKit,
    MediaKit,
    SandboxKit,
    SseWriterKit,
)
from .registry import Harness, register


def _world(h: Harness) -> FakeWorld:
    return build_world(clock=h.clock)


def _use_scenario(world: FakeWorld) -> Any:
    def use(doc: Mapping[str, Any]) -> None:
        world.use_scenario(doc)

    return use


@register("adapter", "fake")
@asynccontextmanager
async def fake_adapters(h: Harness) -> AsyncIterator[AdapterKit]:
    world = _world(h)
    yield AdapterKit(
        adapters=world.adapters,
        clock=h.clock,
        tenant=world.config.current().tenant_ref("t_acme"),
        use_scenario=_use_scenario(world),
        open_streams=lambda: sum(a.open_streams for a in world.adapters.adapters.values()),
    )


@register("decision_engine", "fake")
@asynccontextmanager
async def fake_decisions(h: Harness) -> AsyncIterator[DecisionKit]:
    world = _world(h)

    def db_available(up: bool) -> None:
        world.db.store.available = up

    yield DecisionKit(
        world.decisions,
        world.compiler,
        world.config.current(),
        h.clock,
        _use_scenario(world),
        db_available=db_available,
        outbox_records=world.outbox.of_kind,
    )


@register("question_compiler", "fake")
@asynccontextmanager
async def fake_compiler(h: Harness) -> AsyncIterator[CompilerKit]:
    reg = FakeConfigRegistry()
    yield CompilerKit(FakeQuestionCompiler(reg), reg.current())


@register("decision_cache", "fake")
@asynccontextmanager
async def fake_cache(h: Harness) -> AsyncIterator[CacheKit]:
    world = _world(h)
    cache = FakeDecisionCache(h.clock, world.keys)

    def broken(down: bool) -> None:
        cache.down = down

    yield CacheKit(cache, h.clock, world.keys, broken)


@register("generation_engine", "fake")
@asynccontextmanager
async def fake_generation(h: Harness) -> AsyncIterator[GenerationKit]:
    world = _world(h)
    snap = world.config.current()

    def force(model_id: str, state: str) -> None:
        world.health.force(model_id, snap.tenant_ref("t_acme").home_region, state)  # type: ignore[arg-type]

    yield GenerationKit(world.generation, snap, h.clock, _use_scenario(world), force)


@register("event_log", "fake")
@asynccontextmanager
async def fake_event_log(h: Harness) -> AsyncIterator[EventLogKit]:
    mirror = FakeEventMirror(h.clock)
    yield EventLogKit(FakeTurnEventLog(h.clock, mirror), h.clock, FakeTurnEventLog(h.clock, mirror))


@register("sse_writer", "fake")
@asynccontextmanager
async def fake_sse_writer(h: Harness) -> AsyncIterator[SseWriterKit]:
    yield SseWriterKit(FakeSseWriter(h.clock), h.clock)


@register("file_service", "fake")
@register("input_view_resolver", "fake")
@register("media_negotiator", "fake")
@register("media_budget", "fake")
@asynccontextmanager
async def fake_media(h: Harness) -> AsyncIterator[MediaKit]:
    world = _world(h)

    def pending_uploads(pending: bool) -> None:
        world.files.auto_ready = not pending

    def reject(file_id: str) -> None:
        world.files.set_status(file_id, "rejected", rejection={"reason": "type_mismatch"})

    yield MediaKit(
        world.files,
        world.views,
        world.negotiator,
        world.media_budget,
        example_snapshot(),
        h.clock,
        pending_uploads=pending_uploads,
        reject=reject,
    )


@register("sandbox_runner", "fake")
@asynccontextmanager
async def fake_sandbox(h: Harness) -> AsyncIterator[SandboxKit]:
    yield SandboxKit(FakeSandboxRunner())
