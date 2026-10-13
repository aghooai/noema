"""The composition's lifecycle (spec-01 §3, spec-11 §7) and its DB-backed config registry: start runs each wired
component's steps for its own process in build order, stop bounds the drain (api: grace + 10 s, workers: 10 s) and
closes in reverse order; `ActiveConfigRegistry` loads the active `platform.config_version`, keeps the current snapshot
on a rejected or unreadable version, and is empty (retryable `internal_error`) until the first load."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Final, cast

import anyio
import pytest
from tests.fakes.clock import FakeClock
from tests.fakes.config import EXAMPLE_CONFIG_PATH

from noema.composition import (
    ALL_PROCESSES,
    API,
    JOBS,
    SERVICE_FIELDS,
    ActiveConfigRegistry,
    Process,
    build,
    start,
    stop,
)
from noema.config.loader import digest_of, validate_config
from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.config import ConfigSnapshot
from noema.core.protocols.storage import Database
from noema.db.engine import db_unavailable

pytestmark = [pytest.mark.unit]
ALL_FAKES: Final = frozenset(SERVICE_FIELDS) - {"redis", "runtime"}


async def _record(log: list[str], entry: str) -> None:
    log.append(entry)


def _step(log: list[str], entry: str) -> Callable[[], Any]:
    return lambda: _record(log, entry)


async def test_start_runs_its_process_steps_in_order_and_stop_closes_in_reverse() -> None:
    services = build("test", fakes=ALL_FAKES, environ={})
    lc, log = services._lifecycle, list[str]()
    lc.on_start("a", ALL_PROCESSES, _step(log, "start:a"))
    lc.on_start("b", JOBS, _step(log, "start:b"))
    lc.on_start("c", API, _step(log, "start:c"))
    running = anyio.Event()

    async def task() -> None:
        running.set()
        await anyio.sleep_forever()

    lc.task("t", JOBS, task)
    lc.task("never", API, task)
    lc.on_close("x", _step(log, "close:x"))
    lc.on_close("y", _step(log, "close:y"))
    await start(services, "jobs")
    await running.wait()
    assert log == ["start:a", "start:b"]
    await stop(services)
    assert log == ["start:a", "start:b", "close:y", "close:x"]


async def test_a_failing_start_is_undone() -> None:
    services = build("test", fakes=ALL_FAKES, environ={})
    log: list[str] = []

    async def boom() -> None:
        raise RuntimeError("the broker cannot start")

    services._lifecycle.on_close("x", _step(log, "close:x"))
    services._lifecycle.on_start("boom", ALL_PROCESSES, boom)
    with pytest.raises(RuntimeError):
        await start(services, "api")
    assert log == ["close:x"] and not services.runtime.is_open


@pytest.mark.parametrize(("process", "limit"), [("api", 40.0), ("jobs", 10.0), ("media-worker", 10.0)])
async def test_stop_bounds_the_drain_by_process(process: Process, limit: float) -> None:
    services = build("test", fakes=ALL_FAKES, environ={})
    clock = cast("FakeClock", services.clock)
    async with clock.autojump():  # entered first: stop() leaves the runtime's task group inside it
        await start(services, process)
        services.runtime.on_drain("stuck", lambda grace_s: anyio.sleep_forever())
        begin = clock.monotonic()
        await stop(services)
        assert clock.monotonic() - begin == pytest.approx(limit, abs=0.5)


# --- ActiveConfigRegistry -------------------------------------------------------------------------------------------
def _row(version: int, **patch: Any) -> dict[str, Any]:
    filled, errors = validate_config(EXAMPLE_CONFIG_PATH.read_text(), env="test")
    assert not errors
    filled = {**filled, "config_version": version, **patch}
    return {"version": version, "digest": digest_of(filled), "body": filled, "status": "active"}


@dataclass
class StubDb:
    row: Mapping[str, Any] | None = None
    down: bool = False
    reads: int = 0
    prices: dict[int, Mapping[str, Any]] = field(default_factory=dict)

    @asynccontextmanager
    async def system_uow(self) -> AsyncIterator[StubDb]:
        if self.down:
            raise db_unavailable()
        yield self

    async def active_config(self) -> Mapping[str, Any] | None:
        self.reads += 1
        return self.row

    async def price_catalog(self, version: int) -> Mapping[str, Any] | None:
        return {"version": version, "body": self.prices[version]} if version in self.prices else None


async def test_the_registry_is_empty_until_the_first_load() -> None:
    registry, db = ActiveConfigRegistry("test"), StubDb()
    with pytest.raises(NoemaError) as err:
        registry.current()
    assert err.value.code is ErrorCode.INTERNAL_ERROR
    assert not await registry.refresh(cast("Database", db))  # no active version yet
    db.down = True
    assert not await registry.refresh(cast("Database", db))  # Postgres down: logged, never raised


async def test_refresh_loads_new_versions_and_notifies() -> None:
    registry, db = ActiveConfigRegistry("test"), StubDb(row=_row(1))
    seen: list[int] = []
    registry.on_change(lambda snap: seen.append(snap.version))
    assert await registry.refresh(cast("Database", db))
    assert registry.current().version == 1 and registry.current().digest == db.row["digest"]  # type: ignore[index]
    assert not await registry.refresh(cast("Database", db))  # same version and digest: nothing to do
    db.row = _row(2)
    assert await registry.refresh(cast("Database", db))
    assert registry.current().version == 2 and registry.get(1).version == 1  # the previous version stays pinned
    assert seen == [1, 2]


@pytest.mark.parametrize("bad", ["invalid", "digest"])
async def test_a_rejected_version_keeps_the_current_snapshot(bad: str) -> None:
    registry, db = ActiveConfigRegistry("test"), StubDb(row=_row(1))
    await registry.refresh(cast("Database", db))
    current: ConfigSnapshot = registry.current()
    row = _row(2)
    if bad == "invalid":
        row["body"] = {**row["body"], "routing": {}}  # schema-invalid: rejected by the loader
    else:
        row["digest"] = "sha256:" + "0" * 64
    db.row = row
    assert not await registry.refresh(cast("Database", db))
    assert registry.current() is current
    assert not await registry.refresh(cast("Database", db))  # rejected once, not re-parsed every poll
