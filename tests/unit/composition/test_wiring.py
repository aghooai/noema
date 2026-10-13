"""The real composition (C2b, docs/modules/integration.md): `build()` wires the real implementation of every
`Services` field, shares one retry budget, breaker registry, Redis/S3 client and broker, applies `wrap_adapters`
before the engines capture the registry, refuses the disallowed combinations and needs no `tests` package. Nothing
here opens a connection (build is sync; holders connect in start)."""

from __future__ import annotations

import math
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import fields, is_dataclass
from typing import Any, Final

import pytest

from noema import composition
from noema.composition import ActiveConfigRegistry, StartupError, build
from noema.core.errors import ForbiddenInEnvironment
from noema.core.protocols.adapters import Adapter, AdapterRegistry

pytestmark = [pytest.mark.unit]
ENVIRON: Final = {"NOEMA_REDIS_URL": "redis://127.0.0.1:1/0"}  # never connected: build() opens nothing
REAL_CLASSES: Final = {
    "clock": "SystemClock",
    "ids": "UlidIdGen",
    "config": "ActiveConfigRegistry",
    "secrets": "SecretsManagerProvider",
    "broker": "StsBroker",
    "adapters": "AdapterRegistryImpl",
    "health": "BreakerRegistry",
    "retry_budget": "SlidingRetryBudget",
    "planner": "DefaultWorkGraphPlanner",
    "admission": "GcraAdmission",
    "estimator": "HeuristicTokenEstimator",
    "cost": "LedgerCostMeter",
    "outbox": "WriteBehindOutbox",
    "db": "PgDatabase",
    "jobs": "PgJobQueue",
    "idempotency": "PgIdempotencyStore",
    "events": "InProcessTurnEventLog",
    "sse": "BackpressureSseWriter",
    "files": "MediaFileService",
    "views": "InputViewResolverImpl",
    "negotiator": "MediaNegotiatorImpl",
    "media_budget": "MediaBudgetPlannerImpl",
    "calibrators": "DbCalibratorRegistry",
    "sampler": "Sampler",
    "compiler": "QuestionCompilerImpl",
    "decision_cache": "DecisionCacheImpl",
    "decisions": "DecisionEngineImpl",
    "generation": "StreamingGenerationEngine",
    "authn": "ApiKeyAuthenticator",
    "authz": "MatrixAuthorizer",
    "telemetry": "OtelTelemetry",
    "redis": "MeteredRedis",  # redis.asyncio.Redis with the R4 command metrics (Wave E3)
    "keys": "RedisKeys",
    "s3": "_LazyS3",
    "runtime": "TaskGroupRuntime",
}


def _children(obj: Any) -> Iterator[Any]:
    if isinstance(obj, dict):
        yield from obj.values()
    elif isinstance(obj, (list, tuple, set, frozenset)):
        yield from obj
    else:
        if is_dataclass(obj) and not isinstance(obj, type):
            yield from (getattr(obj, f.name, None) for f in fields(obj))
        yield from getattr(obj, "__dict__", {}).values()
        for slot in getattr(type(obj), "__slots__", ()):
            yield getattr(obj, slot, None)


def refers_to(root: Any, target: Any, depth: int = 4) -> bool:
    """True when `target` is reachable from `root` through attributes, fields and containers (`depth` hops)."""
    seen: set[int] = set()
    frontier = [root]
    for _ in range(depth):
        nxt = []
        for obj in frontier:
            for child in _children(obj):
                if child is target:
                    return True
                if id(child) not in seen and not isinstance(child, (str, bytes, int, float, bool, type(None))):
                    seen.add(id(child))
                    nxt.append(child)
        frontier = nxt
    return False


@pytest.mark.parametrize("env", ["test", "dev", "staging", "prod"])
def test_every_field_is_real(env: str) -> None:
    services = build(env, environ=ENVIRON)  # type: ignore[arg-type]
    assert set(services.wiring.values()) == {"real"}
    assert {f: type(getattr(services, f)).__name__ for f in composition.SERVICE_FIELDS} == REAL_CLASSES


def test_shared_instances() -> None:
    services = build("dev", environ=ENVIRON)
    budget, health = services.retry_budget, services.health
    users: tuple[object, ...] = (services.generation, services.adapters)
    for user in users:
        assert refers_to(user, budget), user  # ONE retry budget: generation's guard and the adapters' ratio
    for user in (services.generation, services.decisions):
        assert refers_to(user, health) and refers_to(user, services.broker)
    for user in (services.outbox, services.idempotency, services.decision_cache, services.admission):  # one client
        assert refers_to(user, services.redis)
    assert refers_to(services.idempotency, services.s3) and refers_to(services.files, services.s3)
    assert refers_to(services.authn, services.db) and refers_to(services.jobs, services.db)
    assert isinstance(services.config, ActiveConfigRegistry)


class Counting:
    """A wrapping AdapterRegistry (the live smoke's CountingAdapterRegistry shape)."""

    def __init__(self, inner: AdapterRegistry) -> None:
        self.inner = inner

    def for_model(self, alias: str) -> Adapter:
        return self.inner.for_model(alias)


def test_wrap_adapters_reaches_every_engine() -> None:
    wrapped: list[Counting] = []

    def wrap(inner: AdapterRegistry) -> AdapterRegistry:
        wrapped.append(Counting(inner))
        return wrapped[-1]

    services = build("dev", environ=ENVIRON, wrap_adapters=wrap)
    [wrapper] = wrapped
    assert services.adapters is wrapper and type(wrapper.inner).__name__ == "AdapterRegistryImpl"
    assert refers_to(services.generation, wrapper) and refers_to(services.decisions, wrapper)


def test_the_ambient_broker_is_dev_test_only() -> None:
    services = build("dev", environ={**ENVIRON, "NOEMA_CREDENTIALS_BROKER": "ambient"})
    assert type(services.broker).__name__ == "AmbientBroker"
    for env in ("staging", "prod"):
        with pytest.raises(ForbiddenInEnvironment):
            build(env, environ={**ENVIRON, "NOEMA_CREDENTIALS_BROKER": "ambient"})
    with pytest.raises(StartupError):
        build("dev", environ={**ENVIRON, "NOEMA_CREDENTIALS_BROKER": "iam-user"})


def test_the_ttft_rule_can_be_disarmed_in_dev_and_test_only() -> None:
    # NOEMA_BREAKER_TTFT_RULE=off: stacks on the wire mock (latencies change 30x between tests); failure ratio armed
    assert build("dev", environ=ENVIRON).health._p.ttft_factor == 3.0  # type: ignore[attr-defined]
    disarmed = build("dev", environ={**ENVIRON, "NOEMA_BREAKER_TTFT_RULE": "off"}).health
    assert disarmed._p.ttft_factor == math.inf and disarmed._p.failure_ratio == 0.5  # type: ignore[attr-defined]
    for env in ("staging", "prod"):
        with pytest.raises(ForbiddenInEnvironment):
            build(env, environ={**ENVIRON, "NOEMA_BREAKER_TTFT_RULE": "off"})
    with pytest.raises(StartupError):
        build("dev", environ={**ENVIRON, "NOEMA_BREAKER_TTFT_RULE": "disabled"})


@pytest.mark.parametrize(
    ("env", "variables"),
    [
        ("prod", {"NOEMA_SANDBOX_BACKEND": "bwrap"}),
        ("dev", {"NOEMA_SANDBOX_BACKEND": "inproc_rlimit"}),
        ("prod", {"NOEMA_AWS_ENDPOINT_OVERRIDES": '{"s3": "http://moto:5000"}'}),
        ("prod", {"NOEMA_CONTRACT_VALIDATE": "1"}),
        ("staging", {"NOEMA_DEV_API_KEY": "ak_test_01J9ZQ7ABCDEFGHJKMNPQRSTVW." + "x" * 43}),
    ],
)
def test_build_keeps_refusing_the_disallowed_combinations(env: str, variables: dict[str, str]) -> None:
    with pytest.raises(ForbiddenInEnvironment):
        build(env, environ={**ENVIRON, **variables})  # type: ignore[arg-type]


def test_the_real_build_needs_no_tests_package(tmp_path: Any) -> None:
    code = (
        "import sys; from noema.composition import build; s = build('dev', environ={}); "
        "print(sorted({m.split('.')[0] for m in sys.modules} & {'tests', 'moto', 'pytest'}), set(s.wiring.values()))"
    )
    out = subprocess.run([sys.executable, "-I", "-c", code], cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split("\n")[0] == "[] {'real'}"
