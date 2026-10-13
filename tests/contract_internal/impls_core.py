"""Registrations: the fakes (and the real implementations that already exist) of the core suites."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from tests.fakes.auth import FakeAuthenticator, FakeAuthorizer
from tests.fakes.clock import FakeIdGen
from tests.fakes.config import FakeConfigRegistry, example_body, example_snapshot, snapshot_from
from tests.fakes.credentials import FakeBroker
from tests.fakes.health import FakeHealthRegistry, FakeRetryBudget
from tests.fakes.labeling import FakeCalibratorRegistry, FakeLabelingSampler
from tests.fakes.ops_admission import FakeAdmission, FakeRateLimiter
from tests.fakes.ops_planning import FakeTokenEstimator, FakeWorkGraphPlanner
from tests.fakes.outbox import FakeCostMeter, FakeOutbox
from tests.fakes.secrets import FakeSecretsProvider
from tests.fakes.telemetry import FakeTelemetry

from noema.config.registry import InMemoryConfigRegistry
from noema.core.clock import SystemClock
from noema.core.ids import UlidIdGen
from noema.core.principal import Principal
from noema.core.protocols.config import ConfigRegistry, ConfigSnapshot
from noema.core.protocols.labeling import Calibrator
from noema.core.protocols.storage import OutboxRecord
from noema.core.runtime import TaskGroupRuntime
from noema.telemetry import OtelTelemetry, setup_telemetry

from .kits import (
    AdmissionKit,
    AuthKit,
    BrokerKit,
    CalibratorKit,
    ClockKit,
    ConfigRegistryKit,
    CostKit,
    EstimatorKit,
    HealthKit,
    IdGenKit,
    PlannerKit,
    RateLimiterKit,
    RuntimeKit,
    SamplerKit,
    SecretsKit,
    TelemetryKit,
)
from .registry import Harness, register


@register("clock", "fake")
@asynccontextmanager
async def fake_clock(h: Harness) -> AsyncIterator[ClockKit]:
    yield ClockKit(h.clock, h.clock.advance)


@register("clock", "system")
@asynccontextmanager
async def system_clock(h: Harness) -> AsyncIterator[ClockKit]:
    yield ClockKit(SystemClock(), None)


@register("id_gen", "fake")
@asynccontextmanager
async def fake_ids(h: Harness) -> AsyncIterator[IdGenKit]:
    yield IdGenKit(FakeIdGen(h.clock), h.clock)


@register("id_gen", "ulid")
@asynccontextmanager
async def ulid_ids(h: Harness) -> AsyncIterator[IdGenKit]:
    yield IdGenKit(UlidIdGen(h.clock), h.clock)


def _versioned(n: int) -> ConfigSnapshot:
    return snapshot_from(example_body(), version=n)


@register("config_registry", "fake")
@asynccontextmanager
async def fake_config(h: Harness) -> AsyncIterator[ConfigRegistryKit]:
    reg = FakeConfigRegistry(_versioned(1))
    yield ConfigRegistryKit(reg, reg.activate, _versioned)


@register("config_registry", "in_memory")
@asynccontextmanager
async def real_config(h: Harness) -> AsyncIterator[ConfigRegistryKit]:
    reg = InMemoryConfigRegistry(_versioned(1))
    yield ConfigRegistryKit(reg, reg.activate, _versioned)


@register("secrets_provider", "fake")
@asynccontextmanager
async def fake_secrets(h: Harness) -> AsyncIterator[SecretsKit]:
    provider = FakeSecretsProvider(h.clock)

    async def put(ref: str, value: bytes, version: str | None) -> None:
        provider.put(ref, value, version=version)

    yield SecretsKit(provider, h.clock, put, lambda: provider.reads, provider.fail)


@register("credential_broker", "fake")
@asynccontextmanager
async def fake_broker(h: Harness) -> AsyncIterator[BrokerKit]:
    reg = FakeConfigRegistry()
    broker = FakeBroker(h.clock, reg)
    yield BrokerKit(
        broker,
        h.clock,
        reg.current().tenant_ref("t_acme"),
        "t_unknown",
        lambda t, c: broker.fail(t, c),  # type: ignore[arg-type]
        lambda t: broker.mints[t],
    )


@register("health_registry", "fake")
@asynccontextmanager
async def fake_health(h: Harness) -> AsyncIterator[HealthKit]:
    yield HealthKit(FakeHealthRegistry(h.clock), FakeRetryBudget(h.clock), h.clock)


@register("work_graph", "fake")
@asynccontextmanager
async def fake_planner(h: Harness) -> AsyncIterator[PlannerKit]:
    yield PlannerKit(FakeWorkGraphPlanner(), example_snapshot())


@register("admission", "fake")
@asynccontextmanager
async def fake_admission(h: Harness) -> AsyncIterator[AdmissionKit]:
    yield AdmissionKit(lambda config: FakeAdmission(h.clock, config, env="test"), h.clock)


@register("token_estimator", "fake")
@asynccontextmanager
async def fake_estimator(h: Harness) -> AsyncIterator[EstimatorKit]:
    yield EstimatorKit(FakeTokenEstimator())


@register("cost_meter", "fake")
@asynccontextmanager
async def fake_cost(h: Harness) -> AsyncIterator[CostKit]:
    def make(config: ConfigRegistry) -> tuple[FakeCostMeter, Any]:
        outbox = FakeOutbox()
        return FakeCostMeter(h.clock, config, outbox), lambda: list(outbox.records)

    yield CostKit(make)


@register("labeling_sampler", "fake")
@asynccontextmanager
async def fake_sampler(h: Harness) -> AsyncIterator[SamplerKit]:
    def make(config: ConfigRegistry) -> tuple[FakeLabelingSampler, Any]:
        outbox = FakeOutbox()
        return FakeLabelingSampler(h.clock, config, outbox), lambda: list[OutboxRecord](outbox.records)

    yield SamplerKit(make)


@register("calibrator_registry", "fake")
@asynccontextmanager
async def fake_calibrators(h: Harness) -> AsyncIterator[CalibratorKit]:
    registry = FakeCalibratorRegistry(FakeConfigRegistry())

    async def add(cal: Calibrator, tenant_id: str | None) -> None:
        registry.add(cal, tenant_id=tenant_id)

    yield CalibratorKit(registry, add, "t_acme")


@register("auth", "fake")
@asynccontextmanager
async def fake_auth(h: Harness) -> AsyncIterator[AuthKit]:
    authn = FakeAuthenticator()

    async def register_key(token: str, p: Principal) -> None:
        authn.register(token, p)

    yield AuthKit(authn, FakeAuthorizer(), register_key)


@register("telemetry", "fake")
@asynccontextmanager
async def fake_telemetry(h: Harness) -> AsyncIterator[TelemetryKit]:
    tel = FakeTelemetry(strict=True)

    def metrics() -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
        out: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        for m in tel.metrics:
            key = (m.name, tuple(sorted(m.labels.items())))
            out[key] = m.value if m.kind == "gauge" else out.get(key, 0.0) + m.value
        return out

    yield TelemetryKit(tel, lambda: [(s.name, dict(s.attributes), s.error) for s in tel.spans], metrics)


@register("telemetry", "otel")
@asynccontextmanager
async def otel_telemetry(h: Harness) -> AsyncIterator[TelemetryKit]:
    setup = setup_telemetry(service_name="noema-test", env="test", memory=True)
    tel = OtelTelemetry(setup, env="test")

    def spans() -> list[tuple[str, Mapping[str, Any], str | None]]:
        assert setup.spans is not None
        out: list[tuple[str, Mapping[str, Any], str | None]] = []
        for s in setup.spans.get_finished_spans():
            attrs = dict(s.attributes or {})
            err = attrs.get("error.type")
            out.append((s.name, attrs, None if err is None else str(err)))
        return out

    def metrics() -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
        assert setup.metrics_memory is not None
        data = setup.metrics_memory.get_metrics_data()
        out: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        for rm in data.resource_metrics if data else ():
            for sm in rm.scope_metrics:
                for metric in sm.metrics:
                    for point in metric.data.data_points:
                        labels = tuple(sorted((k, str(v)) for k, v in (point.attributes or {}).items()))
                        value = getattr(point, "value", None)
                        out[(metric.name, labels)] = float(value if value is not None else getattr(point, "sum", 0.0))
        return out

    try:
        yield TelemetryKit(tel, spans, metrics)
    finally:
        setup.shutdown()


@register("rate_limiter", "fake")
@asynccontextmanager
async def fake_rate_limiter(h: Harness) -> AsyncIterator[RateLimiterKit]:
    yield RateLimiterKit(FakeRateLimiter(h.clock), h.clock)


@register("process_runtime", "task_group")
@asynccontextmanager
async def task_group_runtime(h: Harness) -> AsyncIterator[RuntimeKit]:
    runtime = TaskGroupRuntime(h.clock)

    async def close() -> None:
        await runtime.drain()
        await runtime.close()

    yield RuntimeKit(runtime, h.clock, runtime.open, close, runtime.begin_drain)
    await runtime.close()
