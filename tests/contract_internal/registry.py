"""Implementation registry of the conformance suites (spec-02 §14).

Every suite in `tests/contract_internal/` runs once per registered implementation of its Protocol. The fakes are
registered by `tests/contract_internal/impls_*.py`. **C-wave owners register their real implementation without
editing this directory**: create `conformance_impls.py` anywhere under `tests/` in a path you own (e.g.
`tests/integration/adapters/conformance_impls.py`) and decorate a factory:

    from contextlib import asynccontextmanager
    from tests.contract_internal.registry import Harness, register
    from tests.contract_internal.kits import AdapterKit

    @register("adapter", "converse", marks=(pytest.mark.integration,))
    @asynccontextmanager
    async def converse_adapter(h: Harness) -> AsyncIterator[AdapterKit]:
        ...  # build the implementation (h.fixture("noema_services"), h.run_id, h.clock) and its test hooks
        yield AdapterKit(...)

A factory is an async context manager receiving a `Harness` and yielding the suite's kit (`kits.py` documents each
kit's required hooks; optional hooks may be None, and the tests that need them are skipped). Discovery imports every
`tests/**/conformance_impls.py` at collection time (`discover()`, called by this directory's conftest).
"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, TypeVar

import pytest
from tests.fakes.clock import FakeClock

SUITES: Final[frozenset[str]] = frozenset(
    {
        "clock",
        "id_gen",
        "config_registry",
        "secrets_provider",
        "credential_broker",
        "adapter",
        "health_registry",
        "decision_engine",
        "question_compiler",
        "decision_cache",
        "generation_engine",
        "event_log",
        "sse_writer",
        "idempotency",
        "admission",
        "work_graph",
        "cost_meter",
        "token_estimator",
        "job_queue",
        "outbox",
        "database",
        "input_view_resolver",
        "file_service",
        "sandbox_runner",
        "media_negotiator",
        "media_budget",
        "calibrator_registry",
        "labeling_sampler",
        "auth",
        "telemetry",
        "rate_limiter",
        "process_runtime",
    }
)
IMPLS_FILENAME: Final = "conformance_impls.py"
_TESTS_ROOT: Final = Path(__file__).resolve().parent.parent


@dataclass
class Harness:
    """What a factory gets: the pytest request (use `fixture(name)` for root fixtures such as `noema_services`,
    `noema_run_id` or your own plugin fixtures), the session's run id, and a fresh `FakeClock` the suite drives
    (implementations built on the `Clock` protocol should use it so time-based tests run in virtual time)."""

    request: pytest.FixtureRequest
    run_id: str
    clock: FakeClock = field(default_factory=FakeClock)

    def fixture(self, name: str) -> Any:
        return self.request.getfixturevalue(name)


Factory = Callable[[Harness], AbstractAsyncContextManager[Any]]
F = TypeVar("F", bound=Factory)


@dataclass(frozen=True)
class Implementation:
    """One registered implementation of one suite."""

    suite: str
    name: str
    factory: Factory
    marks: tuple[pytest.MarkDecorator, ...] = ()


_REGISTRY: dict[str, dict[str, Implementation]] = {}


def register(suite: str, name: str, *, marks: Sequence[pytest.MarkDecorator] = ()) -> Callable[[F], F]:
    """Register `factory` as implementation `name` of `suite` (ValueError for unknown suites or duplicates)."""

    def decorate(factory: F) -> F:
        if suite not in SUITES:
            raise ValueError(f"unknown conformance suite {suite!r}; known: {sorted(SUITES)}")
        impls = _REGISTRY.setdefault(suite, {})
        if name in impls:
            raise ValueError(f"{suite}: implementation {name!r} is registered twice")
        impls[name] = Implementation(suite, name, factory, tuple(marks))
        return factory

    return decorate


def implementations(suite: str) -> list[Implementation]:
    """Registered implementations of `suite`, fakes first, then in registration order."""
    impls = list(_REGISTRY.get(suite, {}).values())
    return sorted(impls, key=lambda i: (not i.name.startswith("fake"), impls.index(i)))


def params(suite: str) -> list[Any]:
    """`pytest.param`s for `@pytest.mark.parametrize("impl", params(suite))`."""
    return [pytest.param(impl, id=impl.name, marks=impl.marks) for impl in implementations(suite)]


_DISCOVERED: list[str] = []


def discover(root: Path = _TESTS_ROOT) -> list[str]:
    """Import the built-in registrations and every owner `conformance_impls.py` under `tests/` (once)."""
    if _DISCOVERED:
        return list(_DISCOVERED)
    modules = [
        "tests.contract_internal.impls_core",
        "tests.contract_internal.impls_domain",
        "tests.contract_internal.impls_storage",
    ]
    for path in sorted(root.rglob(IMPLS_FILENAME)):
        rel = path.relative_to(root.parent).with_suffix("")
        if "contract_internal" in rel.parts or "fakes" in rel.parts:
            continue
        modules.append(".".join(rel.parts))
    for module in modules:
        importlib.import_module(module)
        _DISCOVERED.append(module)
    return list(_DISCOVERED)


def kit_fixture(suite: str) -> Any:
    """A parametrized async fixture yielding one kit per implementation of `suite` (use as
    `kit = kit_fixture("clock")` at module level)."""

    @pytest.fixture(params=params(suite))
    async def kit(request: pytest.FixtureRequest, noema_run_id: str) -> AsyncIterator[Any]:
        harness = Harness(request, noema_run_id)
        async with request.param.factory(harness) as value:
            yield value

    return kit


__all__ = [
    "SUITES",
    "Harness",
    "Implementation",
    "asynccontextmanager",
    "discover",
    "implementations",
    "kit_fixture",
    "params",
    "register",
]
