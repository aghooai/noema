"""The composition's background loops: the event-loop lag sampler in every process (`aghoo_event_loop_lag_seconds`,
spec-10 §5, 08 §9), the restart of a loop that raised (`supervised`), the per-process telemetry resource name, the
DSN re-read after a failed pool creation, and a loader bug on a config version rejected like a ConfigError."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any, Final, cast

import anyio
import pytest
from tests.fakes.clock import FakeClock
from tests.fakes.telemetry import FakeTelemetry
from tests.unit.composition.test_lifecycle import StubDb, _row

import noema.composition as composition
from noema.composition import SERVICE_FIELDS, ActiveConfigRegistry, Process, build, start, stop, supervised
from noema.core.errors import NoemaError
from noema.core.protocols.credentials import SecretsProvider
from noema.core.protocols.storage import Database
from noema.db.engine import Engine

pytestmark = [pytest.mark.unit]
ALL_FAKES: Final = frozenset(SERVICE_FIELDS) - {"redis", "runtime"}
LAG: Final = "aghoo_event_loop_lag_seconds"


async def _sampled(telemetry: FakeTelemetry, at_least: float) -> None:
    with anyio.fail_after(5):
        while True:
            if any(m.name == LAG and m.value >= at_least for m in telemetry.metrics):
                return
            await anyio.sleep(0.05)


@pytest.mark.parametrize("process", ["api", "jobs", "media-worker"])
async def test_every_process_samples_the_event_loop_lag(process: Process) -> None:
    services = build("test", fakes=ALL_FAKES - {"clock"}, environ={})  # the real clock: the lag is real time
    telemetry = cast("FakeTelemetry", services.telemetry)
    await start(services, process)
    try:
        await _sampled(telemetry, 0.0)  # the sampler runs (and sleeps again)
        time.sleep(0.7)  # noqa: ASYNC251 - blocks the event loop longer than one 0.5 s sampling period
        await _sampled(telemetry, 0.15)
        assert all(m.kind == "gauge" and not m.labels for m in telemetry.metrics if m.name == LAG)
    finally:
        await stop(services)


async def test_a_loop_that_raised_is_restarted_with_backoff(caplog: pytest.LogCaptureFixture) -> None:
    clock = FakeClock()
    runs: list[float] = []

    async def flaky() -> None:
        runs.append(clock.monotonic())
        if len(runs) < 3:
            raise RuntimeError("one bad iteration")

    with caplog.at_level(logging.ERROR, logger="noema.composition"):
        async with clock.autojump():
            await supervised("flaky", flaky, clock)()  # returns once a run ends normally
    assert [round(t - runs[0], 3) for t in runs] == [0.0, 1.0, 3.0]  # 1 s, then 2 s
    assert sum("background task flaky failed (RuntimeError)" in r.getMessage() for r in caplog.records) == 2


@pytest.mark.parametrize("process", ["api", "jobs", "media-worker"])
def test_the_telemetry_resource_names_the_process(process: Process) -> None:
    services = build("test", fakes=ALL_FAKES - {"telemetry"}, environ={}, process=process)
    setup = services._lifecycle.telemetry_setup
    assert setup is not None
    assert setup.tracer_provider.resource.attributes["service.name"] == f"noema-{process}"


class CountingSecrets:
    def __init__(self) -> None:
        self.reads = 0

    async def get(self, ref: str, *, version: str | None = None) -> bytes:
        self.reads += 1
        return f"postgresql://noema_app:pw{self.reads}@127.0.0.1:1/noema".encode()


async def test_a_failed_pool_creation_reads_the_dsn_secret_again(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(settings: Any) -> Any:
        raise OSError("connection refused")

    monkeypatch.setattr(Engine, "create", refuse)
    clock, secrets = FakeClock(), CountingSecrets()
    pool = composition._DbPool(cast("SecretsProvider", secrets), "noema/db-dsn", clock)
    for _ in range(2):
        with pytest.raises(NoemaError):
            await pool.engine()
        await clock.advance(composition.DB_RETRY_S)
    assert secrets.reads == 2  # a rotated noema/db-dsn is picked up by the next creation


async def test_a_loader_bug_rejects_the_version_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Mapping[str, Any]] = []

    def broken(body: Mapping[str, Any], **kwargs: Any) -> Any:
        calls.append(body)
        raise KeyError("not a ConfigError")

    monkeypatch.setattr(composition, "load_config", broken)
    registry, db = ActiveConfigRegistry("test"), StubDb(row=_row(1))
    assert not await registry.refresh(cast("Database", db))  # logged, never raised (the watcher keeps running)
    assert not await registry.refresh(cast("Database", db))
    assert len(calls) == 1  # rejected once, not re-parsed every poll


async def test_the_app_pool_bounds_every_statement(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wave E edge fix 16: the noema_app pool carries a client command timeout and server statement and lock
    timeouts, so a hung Postgres (a failover without RST, a row-lock wait) fails fast instead of hanging."""
    seen: list[Any] = []

    async def capture(settings: Any) -> Any:
        seen.append(settings)
        raise OSError("connection refused")

    monkeypatch.setattr(Engine, "create", capture)
    pool = composition._DbPool(cast("SecretsProvider", CountingSecrets()), "noema/db-dsn", FakeClock())
    with pytest.raises(NoemaError):
        await pool.engine()
    (settings,) = seen
    assert settings.command_timeout_s == 10.0
    assert dict(settings.server_settings) == {"statement_timeout": "10s", "lock_timeout": "5s"}


def test_the_redis_client_sends_tcp_keepalives() -> None:
    """Wave E edge fix 22: the process's Redis connections (the pub/sub listeners only read) probe a silent peer,
    so a node that died without FIN/RST is noticed by the kernel within about a minute."""
    import socket

    services = build("test", fakes=ALL_FAKES, environ={})  # the real Redis client (it connects lazily)
    kwargs = services.redis.connection_pool.connection_kwargs
    assert kwargs["socket_keepalive"] is True
    assert kwargs["socket_keepalive_options"] == {
        socket.TCP_KEEPIDLE: 30,
        socket.TCP_KEEPINTVL: 10,
        socket.TCP_KEEPCNT: 3,
    }


def test_the_otlp_variables_wire_a_trace_exporter() -> None:
    """Wave E edge fix 28: the standard OTLP variables reach `setup_telemetry`, so traces can be exported."""
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    assert composition.otlp_traces_endpoint({}) is None
    assert (
        composition.otlp_traces_endpoint({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4318/"}) == "http://c:4318/v1/traces"
    )
    assert (
        composition.otlp_traces_endpoint(
            {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4318", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://t/v1/traces"}
        )
        == "http://t/v1/traces"
    )
    services = build("test", fakes=ALL_FAKES - {"telemetry"}, environ={"OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4318"})
    setup = services._lifecycle.telemetry_setup
    assert setup is not None
    try:
        processors = setup.tracer_provider._active_span_processor._span_processors
        exporters = [getattr(p, "span_exporter", None) for p in processors]
        endpoints = [e._endpoint for e in exporters if isinstance(e, OTLPSpanExporter)]
        assert endpoints == ["http://c:4318/v1/traces"]
    finally:
        setup.shutdown()
