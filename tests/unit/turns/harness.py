"""Test harness for the turn orchestrator on the shared fakes (FakeWorld + an in-memory coordinator).

- `MemoryCoordinator` implements `noema.turns.coordination.Coordinator` in memory; two coordinators sharing one
  `CoordinatorState` behave like two pods on one Redis (`state.down = True` = Redis down).
- `RecordingLog` is the fake event log that also records `(monotonic, event)` per append.
- `TurnHarness` builds a world, seeds a session and drives `TurnService` like the route does; `override(name,
  value)` swaps one service the TurnService reads (e.g. a wrapped engine).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, cast

import anyio
from tests.fakes.clock import FakeClock
from tests.fakes.events import FakeEventMirror, FakeTurnEventLog
from tests.fakes.world import FakeWorld, build_world

from noema.contracts.validate import ContractValidator
from noema.core.clock import Clock, Deadline
from noema.core.principal import ALL_SCOPES, Principal, Role
from noema.core.protocols.services import ServiceSet
from noema.core.runtime import TaskGroupRuntime
from noema.core.settings import ProcessSettings
from noema.core.values import JsonValue
from noema.turns.coordination import CoordinationDown, wait_or_timeout
from noema.turns.request import parse_turn_request
from noema.turns.runner import TurnRunner
from noema.turns.service import TurnService

TENANT = "t_acme"
PROJECT = "p1"


def principal(
    principal_id: str = "prn_dev", *, roles: tuple[Role, ...] = ("developer",), project: str = PROJECT
) -> Principal:
    return Principal(TENANT, project, principal_id, "key_1", frozenset(roles), ALL_SCOPES, "req_1")


@dataclass
class CoordinatorState:
    """Shared "Redis" of several MemoryCoordinators."""

    pending: dict[str, str] = field(default_factory=dict)
    queues: dict[str, list[str]] = field(default_factory=dict)
    owners: dict[str, Mapping[str, JsonValue]] = field(default_factory=dict)
    listeners: list[Callable[[str, str], Awaitable[None]]] = field(default_factory=list)
    released: dict[str, anyio.Event] = field(default_factory=dict)
    published: list[tuple[str, str]] = field(default_factory=list)
    down: bool = False

    def event(self, session_id: str) -> anyio.Event:
        return self.released.setdefault(session_id, anyio.Event())

    def wake(self, session_id: str) -> None:
        ev = self.released.pop(session_id, None)
        if ev is not None:
            ev.set()


class MemoryCoordinator:
    """Implements `Coordinator` in memory (see the module docstring)."""

    def __init__(self, clock: Clock, state: CoordinatorState | None = None) -> None:
        self.clock = clock
        self.state = state or CoordinatorState()

    async def session_pending(self, session_id: str) -> bool:
        await anyio.lowlevel.checkpoint()
        return not self.state.down and session_id in self.state.pending

    async def mark_session_pending(self, session_id: str, turn_id: str) -> None:
        if not self.state.down:
            self.state.pending[session_id] = turn_id

    async def queue_join(self, session_id: str, turn_id: str, timeout_ms: int) -> bool:
        if self.state.down:
            return False
        q = self.state.queues.setdefault(session_id, [])
        if turn_id not in q:
            q.append(turn_id)
        return True

    async def queue_head(self, session_id: str, turn_id: str, timeout_ms: int) -> str | None:
        q = self.state.queues.get(session_id)
        return None if self.state.down or not q else q[0]

    async def queue_leave(self, session_id: str, turn_id: str) -> None:
        q = self.state.queues.get(session_id, [])
        if turn_id in q:
            q.remove(turn_id)

    async def wait_released(self, session_id: str, timeout_s: float) -> None:
        await wait_or_timeout(self.state.event(session_id), self.clock, timeout_s)

    async def publish_released(self, tenant_id: str, session_id: str, last_seq: int | None) -> None:
        self.state.wake(session_id)

    async def set_owner(self, turn_id: str, payload: Mapping[str, JsonValue]) -> None:
        if not self.state.down:
            self.state.owners[turn_id] = dict(payload)

    async def refresh_owners(self, payloads: Mapping[str, Mapping[str, JsonValue]]) -> None:
        if not self.state.down:
            self.state.owners.update(payloads)

    async def delete_owner(self, turn_id: str) -> None:
        self.state.owners.pop(turn_id, None)

    async def get_owner(self, turn_id: str) -> Mapping[str, JsonValue] | None:
        if self.state.down:
            raise CoordinationDown("down")
        return self.state.owners.get(turn_id)

    async def publish_cancel(self, tenant_id: str, turn_id: str) -> None:
        if self.state.down:
            raise CoordinationDown("down")
        self.state.published.append((tenant_id, turn_id))
        for listener in list(self.state.listeners):
            await listener(tenant_id, turn_id)

    async def listen(self, on_cancel: Callable[[str, str], Awaitable[None]]) -> None:
        self.state.listeners.append(on_cancel)
        await anyio.sleep_forever()


class RecordingLog(FakeTurnEventLog):
    """`FakeTurnEventLog` that also keeps `(monotonic, event)` per turn."""

    def __init__(self, clock: Clock, mirror: FakeEventMirror | None = None) -> None:
        super().__init__(clock, mirror)
        self.clock = clock
        self.timeline: dict[str, list[tuple[float, dict[str, Any]]]] = {}

    def append(self, turn_id: str, event: Mapping[str, JsonValue]) -> int:
        self.timeline.setdefault(turn_id, []).append((self.clock.monotonic(), dict(event)))
        return super().append(turn_id, event)


class Services:
    """The `ServiceSet` of the tests: the world's fakes plus settings (no Redis: the coordinator is injected)."""

    def __init__(self, world: FakeWorld, **overrides: Any) -> None:
        self.world = world
        self.overrides = overrides
        self.settings = ProcessSettings(
            env="test",
            endpoint_overrides={},
            contract_validate=True,
            expected_pods=1,
            metrics_port=None,
            trusted_proxies=(),
            agent_id=None,
            pod_id="pod-a",
        )
        self.redis: Any = None

    def __getattr__(self, name: str) -> Any:
        if name in self.__dict__.get("overrides", {}):
            return self.overrides[name]
        return getattr(self.world, name)


@dataclass
class TurnHarness:
    """A world, a coordinator and the TurnService on them; `start()` drives a createTurn like the route."""

    world: FakeWorld
    coordinator: MemoryCoordinator
    service: TurnService
    log: RecordingLog

    @property
    def clock(self) -> FakeClock:
        return self.world.fake_clock

    @property
    def services(self) -> ServiceSet:
        return self.service.svc

    def override(self, name: str, value: Any) -> None:
        """Swap the service `name` the TurnService reads (the world keeps its own)."""
        cast(Services, self.service.svc).overrides[name] = value

    @property
    def runtime(self) -> TaskGroupRuntime:
        """The harness's process runtime (opened and closed by the tests)."""
        return cast(TaskGroupRuntime, self.service.svc.runtime)

    def seed_session(
        self,
        session_id: str = "ses_1",
        *,
        last_seq: int = 0,
        sticky_spec: Mapping[str, JsonValue] | None = None,
        owner: str = "prn_dev",
        data_class: str = "internal",
        project: str = PROJECT,
    ) -> None:
        self.world.db.store.seed(
            "session",
            {
                "tenant_id": TENANT,
                "project_id": project,
                "id": session_id,
                "owner_principal_id": owner,
                "data_class": data_class,
                "retention": "default",
                "metadata": {},
                "last_seq": last_seq,
                "sticky_spec": dict(sticky_spec) if sticky_spec is not None else None,
                "active_turn_id": None,
                "lease_expires_at": None,
                "created_at": self.world.clock.now(),
                "deleted_at": None,
                "expires_at": None,
            },
        )

    async def start(
        self,
        body: Mapping[str, Any],
        *,
        who: Principal | None = None,
        t_in_offset_s: float = 0.006,
        deadline_s: float = 120.0,
    ) -> TurnRunner:
        """Steps 6, 8-12 for `body` (ingress t_in = now - t_in_offset_s; the golden fixtures' t_d0 - 6 ms)."""
        who = who or principal()
        request = parse_turn_request(dict(body))
        authorized = await self.service.authorize(who, request)
        turn_id = self.world.ids.new("turn")
        t_in = self.world.clock.monotonic() - t_in_offset_s
        prepared = await self.service.prepare(
            snapshot=self.world.config.current(),
            principal=who,
            request=request,
            authorized=authorized,
            turn_id=turn_id,
            t_in=t_in,
            deadline=Deadline(t_in + deadline_s, self.world.clock),
            trace_id="0" * 32,
        )
        return await self.service.start(prepared)

    def events(self, turn_id: str) -> list[dict[str, Any]]:
        return [e for _, e in self.log.timeline.get(turn_id, [])]

    def timed(self, runner: TurnRunner) -> list[tuple[float, dict[str, Any]]]:
        """(ms from t_d0, event) for the golden comparator."""
        t0 = runner.t_d0 if runner.t_d0 is not None else 0.0
        return [(round((t - t0) * 1000, 3), e) for t, e in self.log.timeline.get(runner.turn_id, [])]


def make_harness(
    *,
    scenario: Mapping[str, Any] | None = None,
    hold_decisions_until_cancel: bool = False,
    state: CoordinatorState | None = None,
    world: FakeWorld | None = None,
) -> TurnHarness:
    """A harness on a fresh FakeClock world (or `world`), with the recording log."""
    if world is None:
        world = build_world(
            clock=FakeClock(), scenario=scenario, hold_decisions_until_cancel=hold_decisions_until_cancel
        )
    log = RecordingLog(world.clock, world.mirror)
    world.events = log
    coordinator = MemoryCoordinator(world.clock, state)
    services = Services(world)
    service = TurnService(cast(ServiceSet, services), coordinator)
    world.runtime.local("turns.service", lambda: service)  # what the routes' TurnService.of() returns
    return TurnHarness(world, coordinator, service, log)


def second_pod(h: TurnHarness) -> TurnHarness:
    """Another API process on the same world: its own event log (sharing the Redis mirror), runtime, coordinator
    (sharing the coordinator state) and TurnService."""
    log = RecordingLog(h.world.clock, h.world.mirror)
    runtime = TaskGroupRuntime(h.world.clock)
    services = Services(h.world, events=log, runtime=runtime)
    coordinator = MemoryCoordinator(h.world.clock, h.coordinator.state)
    service = TurnService(cast(ServiceSet, services), coordinator)
    runtime.local("turns.service", lambda: service)
    return TurnHarness(h.world, coordinator, service, log)


TOKEN = "tok-dev"


class DelayedAuthenticator:
    """The world's authenticator after `delay_s` of virtual time: models the ingress latency the golden fixtures
    assume (t_in = t_d0 - 6 ms)."""

    def __init__(self, inner: Any, clock: Clock, delay_s: float) -> None:
        self.inner = inner
        self.clock = clock
        self.delay_s = delay_s

    async def authenticate(self, headers: Mapping[str, str], client_ip: str) -> Principal:
        await self.clock.sleep(self.delay_s)
        principal_: Principal = await self.inner.authenticate(headers, client_ip)
        return principal_


def app_for(h: TurnHarness, *, token: str = TOKEN, who: Principal | None = None, ingress_delay_s: float = 0.0) -> Any:
    """The API app with the turn routes on the harness's services (response validator on)."""
    from noema.api.app import create_app
    from noema.api.routes.turns import router

    h.world.authn.register(token, who or principal())
    if ingress_delay_s:
        h.override("authn", DelayedAuthenticator(h.world.authn, h.world.clock, ingress_delay_s))
    return create_app(
        services=h.service.svc, routers=[router], validate=True, validator=ContractValidator(), trusted_proxies=()
    )


@asynccontextmanager
async def running(h: TurnHarness) -> AsyncIterator[TurnHarness]:
    """Open the world's runtime and autojump the clock for the block."""
    await h.world.runtime.open()
    try:
        async with h.clock.autojump():
            yield h
    finally:
        await h.world.runtime.close()


USER_BODY: dict[str, Any] = {
    "input": [{"role": "user", "content": [{"type": "input_text", "text": "What is the capital of France?"}]}],
    "stream": True,
}
STICKY_SOL: dict[str, JsonValue] = {
    "generation_profile": "sol-fast",
    "effort": None,
    "max_output_tokens": None,
    "answer_mode": "direct",
    "modalities": ["text"],
}


async def run_turn(
    h: TurnHarness,
    body: Mapping[str, Any] | None = None,
    *,
    who: Principal | None = None,
    deadline_s: float = 120.0,
    during: Callable[[TurnRunner], Coroutine[Any, Any, None]] | None = None,
) -> TurnRunner:
    """Run one turn to completion inside `running(h)`, with an SSE subscriber attached (and `during(runner)`
    concurrently, e.g. a cancel at some instant)."""
    runner = await h.start(body or {**USER_BODY, "session_id": "ses_1"}, who=who, deadline_s=deadline_s)

    async def consume() -> None:
        async for _ in h.world.events.subscribe(runner.turn_id):
            pass

    async with anyio.create_task_group() as tg:
        tg.start_soon(consume)
        if during is not None:
            tg.start_soon(during, runner)
        await runner.done.wait()
    return runner


def types_of(h: TurnHarness, runner: TurnRunner) -> list[str]:
    return [e["type"] for e in h.events(runner.turn_id)]


def first(h: TurnHarness, runner: TurnRunner, kind: str) -> tuple[float, dict[str, Any]]:
    """(ms from t_d0, event) of the first event of `kind`."""
    return next((at, e) for at, e in h.timed(runner) if e["type"] == kind)


def timings(runner: TurnRunner) -> dict[str, Any]:
    """The finished turn's TurnTimings."""
    assert runner.result is not None
    value = runner.result.turn["timings"]
    assert isinstance(value, dict)
    return dict(value)
