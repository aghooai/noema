"""The composition root (spec-01 §3): `build()` wires every `Services` field, refuses the disallowed environment
combinations, and `start`/`stop` run the process's background tasks and its runtime.

Wiring-agnostic on purpose (the integrator replaces fakes with real implementations in C2): the real fields are a
superset of the seed's, the spec fields a subset of `SERVICE_FIELDS`, the staging/prod refusal is asserted for
explicitly requested fakes, and builds without connection settings fake every fakeable field."""

from __future__ import annotations

import logging
import os
import signal
import socket
from types import FrameType

import anyio
import anyio.lowlevel
import httpx
import pytest
from tests.fakes.auth import FakeAuthenticator
from tests.fakes.clock import FakeClock

from noema.composition import (
    COMPONENT_ENVS,
    SERVICE_FIELDS,
    StartupError,
    build,
    check_component,
    process_settings,
    start,
    stop,
)
from noema.core.errors import ErrorCode, ForbiddenInEnvironment, NoemaError
from noema.core.protocols.services import ServiceSet

SPEC_FIELDS = {
    "clock",
    "ids",
    "config",
    "secrets",
    "broker",
    "adapters",
    "health",
    "retry_budget",
    "planner",
    "admission",
    "estimator",
    "cost",
    "outbox",
    "db",
    "jobs",
    "idempotency",
    "events",
    "sse",
    "files",
    "views",
    "negotiator",
    "media_budget",
    "calibrators",
    "sampler",
    "compiler",
    "decision_cache",
    "decisions",
    "generation",
    "authn",
    "authz",
    "telemetry",
    "redis",
    "keys",
    "s3",
}


SEED_REAL = {"clock", "ids", "keys", "telemetry", "redis", "runtime"}
UNFAKEABLE = {"redis", "runtime"}
ALL_FAKES = frozenset(SERVICE_FIELDS) - UNFAKEABLE  # a build that needs no DB, S3 or secret settings
TOKEN = "ak_test_01J9ZQ7ABCDEFGHJKMNPQRSTVW." + "Zm9vYmFyLXNlY3JldC1wYXJ0LW9mLWFuLWFwaS1rZXk"
PROD_ENV = {"NOEMA_REDIS_URL": "redis://redis.internal:6379/0"}


def test_services_have_at_least_the_spec_fields() -> None:
    assert set(SERVICE_FIELDS) >= SPEC_FIELDS | {"runtime"}


async def test_build_start_stop() -> None:
    services = build("test", fakes=ALL_FAKES, environ={"NOEMA_ENV": "test"})
    view: ServiceSet = services
    assert view.runtime is services.runtime
    assert services.keys.ns.startswith("ns:") and services.keys.ns.endswith(":")
    assert {f for f, kind in services.wiring.items() if kind == "real"} >= UNFAKEABLE
    assert services.config.current().tenant("t_acme").home_region == "us-east-1"
    await start(services, "api")
    assert (services.runtime.is_open, services.runtime.draining) == (True, False)
    with pytest.raises(RuntimeError):
        await start(services, "api")
    await anyio.lowlevel.checkpoint()
    await stop(services)
    assert (services.runtime.draining, services.runtime.is_open) == (True, False)
    await stop(services)  # idempotent


def test_the_seed_real_fields_stay_real() -> None:
    services = build("test", environ=dict(os.environ))
    assert {f for f, kind in services.wiring.items() if kind == "real"} >= SEED_REAL


async def test_fakes_can_replace_real_fields() -> None:
    services = build("test", fakes=frozenset({"clock", "telemetry"}), environ={})
    assert isinstance(services.clock, FakeClock) and services.wiring["telemetry"] == "fake"
    await stop(services)


async def test_stop_waits_for_the_drain_hooks() -> None:
    services = build("test", fakes=ALL_FAKES, environ={})
    await start(services, "jobs")
    graces: list[float] = []

    async def hook(grace_s: float) -> None:
        graces.append(grace_s)
        await anyio.lowlevel.checkpoint()

    services.runtime.on_drain("test.hook", hook)
    await stop(services)
    assert graces == [30.0]
    with pytest.raises(RuntimeError):
        services.runtime.spawn("late", anyio.lowlevel.checkpoint)


@pytest.mark.parametrize(
    ("env", "environ", "error"),
    [
        ("prod", {}, StartupError),  # NOEMA_REDIS_URL is required outside dev/test
        ("dev", {"NOEMA_ENV": "test"}, StartupError),
        ("qa", {}, StartupError),
        ("dev", {"NOEMA_AWS_ENDPOINT_OVERRIDES": '{"bogus": "http://127.0.0.1:1"}'}, StartupError),
        ("dev", {"NOEMA_AWS_ENDPOINT_OVERRIDES": '{"sts": "ftp://x"}'}, StartupError),
        ("dev", {"NOEMA_CONTRACT_VALIDATE": "yes"}, StartupError),
        ("dev", {"NOEMA_EXPECTED_PODS": "0"}, StartupError),
        ("dev", {"NOEMA_TRUSTED_PROXIES": "not-a-cidr"}, StartupError),
    ],
)
def test_build_refuses(env: str, environ: dict[str, str], error: type[Exception]) -> None:
    with pytest.raises(error):
        build(env, environ=environ)  # type: ignore[arg-type]


@pytest.mark.parametrize("env", ["prod", "staging"])
def test_requested_fakes_are_refused_outside_dev_test(env: str) -> None:
    with pytest.raises(ForbiddenInEnvironment):
        build(env, fakes=frozenset({"adapters"}), environ=PROD_ENV)  # type: ignore[arg-type]


def test_unknown_or_unfakeable_fields() -> None:
    with pytest.raises(StartupError):
        build("test", fakes=frozenset({"nope"}), environ={})
    for name in UNFAKEABLE:
        with pytest.raises(StartupError):
            build("test", fakes=frozenset({name}), environ={})


@pytest.mark.parametrize(
    ("settings_env", "environ", "error"),
    [
        ("prod", {"NOEMA_AWS_ENDPOINT_OVERRIDES": '{"sts": "http://127.0.0.1:25000"}'}, ForbiddenInEnvironment),
        ("staging", {"NOEMA_AWS_ENDPOINT_OVERRIDES": '{"s3": "https://minio.local"}'}, ForbiddenInEnvironment),
        ("prod", {"NOEMA_CONTRACT_VALIDATE": "1"}, ForbiddenInEnvironment),
    ],
)
def test_process_settings_refusals(settings_env: str, environ: dict[str, str], error: type[Exception]) -> None:
    with pytest.raises(error):
        process_settings(settings_env, environ)


def test_process_settings_accepts_dev_overrides() -> None:
    s = process_settings(
        "dev",
        {
            "NOEMA_AWS_ENDPOINT_OVERRIDES": '{"sts": "http://127.0.0.1:25000"}',
            "NOEMA_CONTRACT_VALIDATE": "1",
            "NOEMA_EXPECTED_PODS": "3",
            "NOEMA_TRUSTED_PROXIES": "10.0.0.0/8, ::1/128",
        },
    )
    assert s.endpoint_overrides == {"sts": "http://127.0.0.1:25000"} and s.contract_validate
    assert s.expected_pods == 3 and len(s.trusted_proxies) == 2


@pytest.mark.parametrize(
    ("kind", "env", "allowed"),
    [
        ("broker.ambient", "dev", True),
        ("broker.ambient", "test", True),
        ("broker.ambient", "staging", False),
        ("broker.ambient", "prod", False),
        ("sandbox.inproc_rlimit", "test", True),
        ("sandbox.inproc_rlimit", "dev", False),
        ("sandbox.bwrap", "dev", True),
        ("sandbox.bwrap", "prod", False),
        ("sandbox.remote", "prod", True),
        ("fake", "prod", False),
        ("broker.sts", "prod", True),
    ],
)
def test_component_env_matrix(kind: str, env: str, allowed: bool) -> None:
    assert kind in COMPONENT_ENVS
    if allowed:
        check_component(kind, env)
    else:
        with pytest.raises(ForbiddenInEnvironment):
            check_component(kind, env)


def test_connection_settings() -> None:
    s = process_settings("dev", {})
    assert s.pod_id == socket.gethostname() and s.s3_bucket == "noema-dev" and s.db_dsn_ref == "noema/db-dsn"
    assert s.redis_url.startswith("redis://") and s.drain_delay_s == 5.0 and s.dev_api_key is None
    s = process_settings(
        "prod",
        {**PROD_ENV, "NOEMA_POD_ID": "api-7", "NOEMA_S3_BUCKET": "b1", "NOEMA_DRAIN_DELAY_S": "2.5"},
    )
    assert (s.pod_id, s.s3_bucket, s.redis_url, s.drain_delay_s) == ("api-7", "b1", PROD_ENV["NOEMA_REDIS_URL"], 2.5)
    assert process_settings("test", {}).drain_delay_s == 0.0


@pytest.mark.parametrize(
    ("env", "environ", "error"),
    [
        ("prod", {**PROD_ENV, "NOEMA_DEV_API_KEY": TOKEN}, ForbiddenInEnvironment),
        ("dev", {"NOEMA_DEV_API_KEY": "ak_test_short.secret"}, StartupError),
        ("dev", {"NOEMA_REDIS_URL": "http://x"}, StartupError),
        ("dev", {"NOEMA_DRAIN_DELAY_S": "-1"}, StartupError),
    ],
)
def test_connection_setting_refusals(env: str, environ: dict[str, str], error: type[Exception]) -> None:
    with pytest.raises(error):
        process_settings(env, environ)


async def test_the_dev_key_authenticates_on_a_seed_build() -> None:
    services = build("test", fakes=ALL_FAKES, environ={"NOEMA_DEV_API_KEY": TOKEN})
    assert isinstance(services.authn, FakeAuthenticator)
    who = await services.authn.authenticate({"Authorization": f"Bearer {TOKEN}"}, "127.0.0.1")
    assert (who.tenant_id, who.principal_id, who.key_id) == ("t_acme", "prn_dev", "01J9ZQ7ABCDEFGHJKMNPQRSTVW")
    assert "secret" not in repr(services.settings)
    without = build("test", fakes=ALL_FAKES, environ={})
    with pytest.raises(NoemaError) as err:
        await without.authn.authenticate({"Authorization": f"Bearer {TOKEN}"}, "127.0.0.1")
    assert err.value.code is ErrorCode.INVALID_API_KEY
    await stop(services)
    await stop(without)


async def test_sigterm_starts_draining_then_forwards_to_the_previous_handler() -> None:
    forwarded: list[int] = []

    def previous(signum: int, frame: FrameType | None) -> None:
        forwarded.append(signum)

    original = signal.signal(signal.SIGTERM, previous)
    try:
        services = build("test", fakes=ALL_FAKES, environ={})
        await start(services, "api")
        signal.raise_signal(signal.SIGTERM)
        for _ in range(100):
            if forwarded:
                break
            await anyio.lowlevel.checkpoint()
        assert services.runtime.draining and forwarded == [signal.SIGTERM]
        await stop(services)
        assert signal.getsignal(signal.SIGTERM) is previous  # restored by stop()
    finally:
        signal.signal(signal.SIGTERM, original)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def test_metrics_are_served_on_the_metrics_port() -> None:
    port = _free_port()
    services = build("dev", fakes=ALL_FAKES - {"telemetry"}, environ={"NOEMA_METRICS_PORT": str(port)})
    await start(services, "jobs")
    try:
        services.telemetry.instruments.add("aghoo_spec_restarts_total", profile="sol-fast")
        async with httpx.AsyncClient() as client:
            response = await client.get(f"http://127.0.0.1:{port}/metrics")
        assert response.status_code == 200 and b"aghoo_spec_restarts_total" in response.content
    finally:
        await stop(services)
    with pytest.raises(httpx.ConnectError):
        async with httpx.AsyncClient() as client:
            await client.get(f"http://127.0.0.1:{port}/metrics")


async def test_outside_test_every_noema_logger_is_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    services = build("dev", fakes=ALL_FAKES - {"telemetry"}, environ={})
    await start(services, "jobs")
    try:
        logging.getLogger("noema.api.access").warning("auth Bearer abcdefghijkl0123 from %s", "client")
    finally:
        await stop(services)
    err = capsys.readouterr().err
    assert "[REDACTED:bearer]" in err and "abcdefghijkl0123" not in err and '"logger":"noema.api.access"' in err
    assert root.handlers == before  # stop() restores the previous handlers
