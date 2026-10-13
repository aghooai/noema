"""The composition root (spec-01 §3): `Services`, `build(env, fakes=…)`, `start(services, process)`, `stop(services)`.

`build()` wires the real implementation of every `Services` field (C2b, docs/modules/integration.md); `fakes` names
fields that get their `tests/fakes` implementation instead (via `tests.fakes.world.build_world`, imported at build
time only, so fakes exist only where the `tests` package is importable and only in `test`/`dev`). `wrap_adapters`
wraps the adapter registry before the decision, generation and labeling engines capture it (the live smoke's call
counter). In dev/test, `NOEMA_DEV_API_KEY` registers one documented key on the FAKE authenticator only (principal
`prn_dev` of tenant `t_acme`, project `prj_dev`, roles developer + tenant_admin); with the real authenticator mint
keys with `noema-admin keys create --env test` (tools/bootstrap_dev.py does it for a dev/e2e stack).

Construction rule: `build()` stays sync and opens no connection. What needs async construction is a holder that
implements its Protocol and connects in `start()` (or on first use): the S3 client (`_LazyS3`), the noema_app pool
behind `services.db` (`_DbPool`, DSN from the platform secret `settings.db_dsn_ref`), the LISTEN connections
(`_DeferredListener`) and the platform config (`ActiveConfigRegistry`: empty until the active
`platform.config_version` loads; `/readyz` is 503 until then). Each real component registers its start steps,
background tasks, drain hooks and close steps for the processes that run it (`_Lifecycle`); `start(services,
process)` runs them in build order, `stop()` closes them in reverse order.

Shutdown (spec-11 §7): in the api process `start()` chains SIGTERM: the runtime starts draining at once (`/readyz`
503, drain hooks get the 30 s grace), and uvicorn's own handler runs `drain_delay_s` later (run uvicorn with
`--timeout-graceful-shutdown` above the grace). `stop()` then waits for the drain hooks (api: grace + 10 s, so every
turn finalized; jobs and media-worker: 10 s for the running handlers), flushes the outbox, cancels the background
tasks and closes the clients. With `NOEMA_METRICS_PORT` set, `start()` serves the Prometheus exposition there.

Several api processes per pod (E sprint B; uvicorn `--workers`, which defaults to `WEB_CONCURRENCY`): with
`WEB_CONCURRENCY` = N > 1 every process is its own pod in the leases, owners and consumer names (`pod_id` =
`<NOEMA_POD_ID or host name>-<pid>`), the local fallback shares divide by pods x workers (`expected_pods` =
NOEMA_EXPECTED_PODS x N) and each process serves its metrics on the first free port of `NOEMA_METRICS_PORT` ..
`NOEMA_METRICS_PORT + N - 1` (one scrape target per worker).

`build()` validates the spec-05 §5 process environment and refuses the disallowed combinations: endpoint overrides
(`NOEMA_AWS_ENDPOINT_OVERRIDES`) outside dev/test, `NOEMA_CONTRACT_VALIDATE` in prod, and every wired component
whose kind is not allowed in `env` (`COMPONENT_ENVS`: fakes and AmbientBroker (`NOEMA_CREDENTIALS_BROKER=ambient`)
only in dev/test, the bwrap sandbox only in dev/test and InprocRlimit only in test, ADR-001 §7-8, ADR-002 §7,
IMPL-041; the disarmed breaker TTFT rule `NOEMA_BREAKER_TTFT_RULE=off` only in dev/test); violations raise
`ForbiddenInEnvironment`.
"""

from __future__ import annotations

import functools
import gc
import importlib
import ipaddress
import json
import logging
import math
import os
import random
import re
import signal
import socket
import threading
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from types import FrameType, SimpleNamespace
from typing import TYPE_CHECKING, Any, Final, Literal, cast, get_args
from urllib.parse import urlparse

import anyio
from aiobotocore.config import AioConfig

from noema.config.loader import ConfigError, load_config, referenced_price_version
from noema.config.registry import InMemoryConfigRegistry
from noema.core.clock import Clock, SystemClock
from noema.core.errors import ErrorCode, ForbiddenInEnvironment, NoemaError
from noema.core.ids import IdGen, UlidIdGen
from noema.core.objects import default_bucket
from noema.core.principal import ALL_SCOPES, Principal
from noema.core.protocols.adapters import AdapterRegistry
from noema.core.protocols.auth import Authenticator, Authorizer
from noema.core.protocols.config import ConfigRegistry, ConfigSnapshot
from noema.core.protocols.credentials import CredentialBroker, SecretsProvider
from noema.core.protocols.decision import DecisionCache, DecisionEngine, QuestionCompiler
from noema.core.protocols.events import SseWriter, TurnEventLog
from noema.core.protocols.generation import GenerationEngine
from noema.core.protocols.labeling import CalibratorRegistry, LabelingSampler
from noema.core.protocols.media import FileService, InputViewResolver, MediaBudgetPlanner, MediaNegotiator
from noema.core.protocols.ops import (
    Admission,
    CostMeter,
    HealthRegistry,
    RetryBudget,
    TokenEstimator,
    WorkGraphPlanner,
)
from noema.core.protocols.storage import Database, IdempotencyStore, JobQueue, Outbox
from noema.core.protocols.telemetry import Instruments, Telemetry
from noema.core.pubsub import heartbeat_messages
from noema.core.redis_keys import RedisKeys
from noema.core.runtime import DRAIN_GRACE_S, TaskGroupRuntime
from noema.core.secret import SecretStr
from noema.core.settings import Env, ProcessSettings, SandboxBackend
from noema.db.engine import Engine, PooledConnection, app_pool_settings, db_unavailable
from noema.identity.aws import PLATFORM_REGION, LazyClient
from noema.ops.notify_bridge.listener import PgListener

if TYPE_CHECKING:
    import redis.asyncio
    from types_aiobotocore_s3 import S3Client

    from noema.core.protocols.services import ServiceSet
    from noema.telemetry import TelemetrySetup

Process = Literal["api", "jobs", "media-worker"]
ENVS: Final[frozenset[str]] = frozenset(get_args(Env))
PROCESSES: Final[frozenset[str]] = frozenset(get_args(Process))
ENDPOINT_OVERRIDE_KEYS: Final = frozenset({"sts", "secretsmanager", "s3", "bedrock-runtime", "mantle"})
DEV_TEST: Final = frozenset({"dev", "test"})
ALL_ENVS: Final = frozenset({"test", "dev", "staging", "prod"})
# Allowed NOEMA_ENV per wired component kind (ADR-001 §7, ADR-002 §7, IMPL-041).
COMPONENT_ENVS: Final[Mapping[str, frozenset[str]]] = {
    "fake": DEV_TEST,
    "real": ALL_ENVS,
    "broker.sts": ALL_ENVS,
    "broker.ambient": DEV_TEST,
    "sandbox.remote": ALL_ENVS,
    "sandbox.nsjail": ALL_ENVS,
    "sandbox.bwrap": DEV_TEST,
    "sandbox.inproc_rlimit": frozenset({"test"}),
    "contract_validator": frozenset({"test", "dev", "staging"}),
    "breaker.ttft_off": DEV_TEST,
}
CALIBRATOR_POLL_S: Final = 5.0
LOOP_LAG_EVERY_S: Final = 0.5  # the aghoo_event_loop_lag_seconds sampler's period (every process)
POOL_GAUGES_EVERY_S: Final = 1.0  # the aghoo_{db,redis}_pool_{size,in_use} sampler's period (R4, Wave E3)
REDIS_MAX_CONNECTIONS: Final = 256  # NOEMA_REDIS_MAX_CONNECTIONS default: one process's pool, pub/sub included
REDIS_POOL_TIMEOUT_S: Final = 0.5  # the longest wait for a free pooled Redis connection (then ConnectionError)
# R4: the share of Redis commands, Redis connection acquires and Postgres units of work whose latency histograms are
# observed (each drawn at random): one OTel histogram record costs ~15 us, a Redis command ~100 us of CPU, so timing
# every one added ~34 us per command (reports/qa/e3/misc-fixes.md); the pool gauges count every connection.
DATASTORE_SAMPLE_RATE: Final = 1 / 16
_SAMPLER: Final = random.Random()  # noqa: S311 - telemetry sampling, not cryptography
CONFIG_POLL_S: Final = 5.0  # the active platform.config_version poll (spec-05 §3), next to config_changed
OUTBOX_FLUSH_S: Final = 10.0
API_DRAIN_SLACK_S: Final = 10.0  # stop(): turns get the grace, then SHUTDOWN_STOP_S to finalize (orchestrator 14)
WORKER_DRAIN_S: Final = 10.0  # jobs / media-worker: running handlers get 10 s (spec-11 §7)
DB_RETRY_S: Final = 1.0  # a failed DSN read / pool creation is retried at most once a second
LISTEN_BACKOFF_MAX_S: Final = 30.0
BROKERS: Final = frozenset({"sts", "ambient"})  # NOEMA_CREDENTIALS_BROKER (ambient: dev/test, ADR-001 §7)
QUIET_LOGGERS: Final = ("httpx", "httpcore")  # set to WARNING by the process log handler (outside test)
_log: Final = logging.getLogger("noema.composition")
MAX_WORKERS: Final = 64  # WEB_CONCURRENCY: api processes per pod
PYTHON_GC_THRESHOLDS: Final = (700, 10, 10)  # CPython's defaults
GC_THRESHOLDS: Final = (700, 10, 100)  # set by start() outside test (`_freeze_startup_heap`)
DEFAULT_DRAIN_DELAY_S: Final = 5.0  # SIGTERM -> uvicorn shutdown outside test (readyz 503 first, spec-11 §7)
API_KEY_FORMAT: Final = re.compile(r"ak_(?:live|test)_([0-9A-HJKMNP-TV-Z]{26})\.[A-Za-z0-9_-]{43}")  # spec-09 §2
DEV_TENANT_ID: Final = "t_acme"  # the example config's tenant (the seed config registry)
DEV_PRINCIPAL_ID: Final = "prn_dev"
DEV_PROJECT_ID: Final = "prj_dev"


class StartupError(ValueError):
    """An invalid process setting (spec-05 §5): the process must not start."""


def check_component(kind: str, env: str) -> None:
    """Raise `ForbiddenInEnvironment` unless a component of `kind` may run in `env` (unknown kinds: ValueError)."""
    allowed = COMPONENT_ENVS.get(kind)
    if allowed is None:
        raise ValueError(f"unknown component kind {kind!r}")
    if env not in allowed:
        raise ForbiddenInEnvironment(f"{kind} is not allowed with NOEMA_ENV={env}")


def _overrides(raw: str, env: str) -> dict[str, str]:
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        raise StartupError("NOEMA_AWS_ENDPOINT_OVERRIDES is not JSON") from None
    if not isinstance(value, dict):
        raise StartupError("NOEMA_AWS_ENDPOINT_OVERRIDES must be a JSON object")
    if unknown := set(value) - ENDPOINT_OVERRIDE_KEYS:
        raise StartupError(f"unknown endpoint override keys: {sorted(unknown)}")
    for key, url in value.items():
        parsed = urlparse(url) if isinstance(url, str) else None
        if parsed is None or parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise StartupError(f"endpoint override {key!r} must be an http(s) URL")
    if value and env not in DEV_TEST:
        raise ForbiddenInEnvironment(f"endpoint overrides are not allowed with NOEMA_ENV={env} (ADR-001 §8)")
    return {str(k): str(v) for k, v in value.items()}


def _int(environ: Mapping[str, str], name: str, *, low: int, high: int) -> int | None:
    raw = environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        raise StartupError(f"{name} must be an integer") from None
    if not low <= value <= high:
        raise StartupError(f"{name} must be in [{low}, {high}]")
    return value


def _float(environ: Mapping[str, str], name: str, *, low: float, high: float) -> float | None:
    raw = environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        raise StartupError(f"{name} must be a number") from None
    if not low <= value <= high:
        raise StartupError(f"{name} must be in [{low}, {high}]")
    return value


def _redis_url(environ: Mapping[str, str], env: str) -> str:
    url = environ.get("NOEMA_REDIS_URL", "").strip()
    if not url and env in DEV_TEST:
        url = environ.get("NOEMA_TEST_REDIS_URL", "").strip() or "redis://127.0.0.1:26379/0"
    if not url:
        raise StartupError("NOEMA_REDIS_URL is required outside dev/test")
    if urlparse(url).scheme not in ("redis", "rediss", "unix"):
        raise StartupError("NOEMA_REDIS_URL must be a redis://, rediss:// or unix:// URL")
    return url


def _drain_delay(environ: Mapping[str, str], env: str) -> float:
    value = _float(environ, "NOEMA_DRAIN_DELAY_S", low=0, high=120)
    if value is None:
        return 0.0 if env == "test" else DEFAULT_DRAIN_DELAY_S
    return value


def _sandbox(environ: Mapping[str, str], env: str) -> tuple[SandboxBackend, str, str]:
    backend = environ.get("NOEMA_SANDBOX_BACKEND", "").strip() or "remote"
    if backend not in get_args(SandboxBackend):
        raise StartupError(f"NOEMA_SANDBOX_BACKEND must be one of {sorted(get_args(SandboxBackend))}")
    check_component(f"sandbox.{backend}", env)
    spool = environ.get("NOEMA_SPOOL_DIR", "").strip() or "/spool"
    socket_path = environ.get("NOEMA_SANDBOX_SOCKET", "").strip() or os.path.join(spool, "sandboxd.sock")
    for name, path in (("NOEMA_SPOOL_DIR", spool), ("NOEMA_SANDBOX_SOCKET", socket_path)):
        if not os.path.isabs(path):
            raise StartupError(f"{name} must be an absolute path")
    return cast("SandboxBackend", backend), spool, socket_path


def _estimator_table(environ: Mapping[str, str]) -> str | None:
    path = environ.get("NOEMA_TOKEN_ESTIMATOR_TABLE", "").strip()
    if path and not os.path.isabs(path):
        raise StartupError("NOEMA_TOKEN_ESTIMATOR_TABLE must be an absolute path")
    return path or None


def _dev_api_key(environ: Mapping[str, str], env: str) -> SecretStr | None:
    raw = environ.get("NOEMA_DEV_API_KEY", "").strip()
    if not raw:
        return None
    if env not in DEV_TEST:
        raise ForbiddenInEnvironment(f"NOEMA_DEV_API_KEY is not allowed with NOEMA_ENV={env}")
    if API_KEY_FORMAT.fullmatch(raw) is None:
        raise StartupError("NOEMA_DEV_API_KEY must be ak_test_<26 Crockford chars>.<43 base64url chars>")
    return SecretStr(raw, register=True)


def broker_kind(environ: Mapping[str, str], env: str) -> str:
    """`NOEMA_CREDENTIALS_BROKER`: `sts` (default, every env) or `ambient` (the developer's own credentials for the
    dev live smoke, ADR-001 §7; dev/test only, `check_component("broker.ambient", env)`)."""
    kind = environ.get("NOEMA_CREDENTIALS_BROKER", "").strip() or "sts"
    if kind not in BROKERS:
        raise StartupError(f"NOEMA_CREDENTIALS_BROKER must be one of {sorted(BROKERS)}")
    check_component(f"broker.{kind}", env)
    return kind


def ttft_rule_armed(environ: Mapping[str, str], env: str) -> bool:
    """`NOEMA_BREAKER_TTFT_RULE`: `on` (default) or `off`, which disarms the breakers' p95-TTFT rule (design 04 §2)
    for stacks on the wire mock, whose scenarios change a model's latency 30x from one test to the next; dev/test
    only (`check_component("breaker.ttft_off", env)`). The failure-ratio rule stays armed."""
    value = environ.get("NOEMA_BREAKER_TTFT_RULE", "").strip() or "on"
    if value not in ("on", "off"):
        raise StartupError("NOEMA_BREAKER_TTFT_RULE must be on or off")
    if value == "off":
        check_component("breaker.ttft_off", env)
    return value == "on"


def api_workers(environ: Mapping[str, str]) -> int:
    """The api processes of one pod: `WEB_CONCURRENCY` (uvicorn's default for `--workers`), 1..64, default 1."""
    return _int(environ, "WEB_CONCURRENCY", low=1, high=MAX_WORKERS) or 1


def process_settings(env: str, environ: Mapping[str, str] | None = None) -> ProcessSettings:
    """Validate the spec-05 §5 variables and the connection settings for `env` (`environ` defaults to
    `os.environ`; see `ProcessSettings` for each variable). Raises `StartupError` for invalid values and
    `ForbiddenInEnvironment` for disallowed combinations."""
    if env not in ENVS:
        raise StartupError(f"NOEMA_ENV must be one of {sorted(ENVS)}")
    environ = os.environ if environ is None else environ
    declared = environ.get("NOEMA_ENV", "").strip()
    if declared and declared != env:
        raise StartupError(f"build(env={env!r}) but NOEMA_ENV={declared!r}")
    validate = environ.get("NOEMA_CONTRACT_VALIDATE", "").strip()
    if validate not in ("", "1"):
        raise StartupError("NOEMA_CONTRACT_VALIDATE must be 1 or unset")
    if validate == "1":
        check_component("contract_validator", env)
    sandbox_backend, spool_dir, sandbox_socket = _sandbox(environ, env)
    proxies: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for item in environ.get("NOEMA_TRUSTED_PROXIES", "").replace(" ", "").split(","):
        if item:
            try:
                proxies.append(ipaddress.ip_network(item, strict=False))
            except ValueError:
                raise StartupError("NOEMA_TRUSTED_PROXIES must be a comma-separated CIDR list") from None
    workers = api_workers(environ)
    pod_id = environ.get("NOEMA_POD_ID", "").strip() or socket.gethostname()
    if workers > 1:  # the workers of one pod must not share leases, turn owners or the outbox consumer name
        pod_id = f"{pod_id}-{os.getpid()}"
    return ProcessSettings(
        env=cast("Env", env),
        endpoint_overrides=_overrides(environ.get("NOEMA_AWS_ENDPOINT_OVERRIDES", ""), env),
        contract_validate=validate == "1",
        expected_pods=(_int(environ, "NOEMA_EXPECTED_PODS", low=1, high=10_000) or 1) * workers,
        metrics_port=_int(environ, "NOEMA_METRICS_PORT", low=1, high=65_535),
        trusted_proxies=tuple(proxies),
        agent_id=environ.get("NOEMA_AGENT_ID") or None,
        pod_id=pod_id,
        s3_bucket=environ.get("NOEMA_S3_BUCKET", "").strip() or default_bucket(env),
        redis_url=_redis_url(environ, env),
        db_dsn_ref=environ.get("NOEMA_DB_DSN_REF", "").strip() or "noema/db-dsn",
        drain_delay_s=_drain_delay(environ, env),
        dev_api_key=_dev_api_key(environ, env),
        sandbox_backend=sandbox_backend,
        spool_dir=spool_dir,
        sandbox_socket=sandbox_socket,
        redis_max_connections=_int(environ, "NOEMA_REDIS_MAX_CONNECTIONS", low=8, high=10_000) or REDIS_MAX_CONNECTIONS,
        token_estimator_table=_estimator_table(environ),
    )


# --- holders: what needs async construction behind the sync build() --------------------------------------------------

S3_POOL_CONNECTIONS: Final = 64
DB_POOL_MAX: Final = 20


class _LazyS3:
    """`services.s3`: the platform aiobotocore S3 client (pod credentials; the dev/test `s3` endpoint override with
    path-style addressing), entered by `start()` or on first use. Every other attribute is an async client method
    forwarded to the entered client (`await s3.put_object(...)`); `close()` leaves it."""

    def __init__(self, client: LazyClient) -> None:
        self._client = client

    def __getattr__(self, name: str) -> Callable[..., Awaitable[Any]]:
        if name.startswith("_"):
            raise AttributeError(name)

        async def call(*args: Any, **kwargs: Any) -> Any:
            client = await self._client.get()
            return await getattr(client, name)(*args, **kwargs)

        return call

    async def open(self) -> None:
        await self._client.get()

    async def close(self) -> None:
        await self._client.aclose()


def _s3_client(settings: ProcessSettings) -> _LazyS3:
    endpoint = settings.endpoint_overrides.get("s3")
    config = AioConfig(
        connect_timeout=2.0,
        read_timeout=30.0,
        retries={"mode": "standard", "max_attempts": 3},
        max_pool_connections=S3_POOL_CONNECTIONS,
        s3={"addressing_style": "path"} if endpoint else None,  # moto/minio hosts have no bucket subdomains
    )
    return _LazyS3(LazyClient("s3", region=PLATFORM_REGION, endpoint_url=endpoint, config=config))


def _sampled() -> bool:
    """One draw of `DATASTORE_SAMPLE_RATE` (R4)."""
    return _SAMPLER.random() < DATASTORE_SAMPLE_RATE


class _DbPool:
    """The noema_app pool behind `services.db` (`PgDatabase(_DbPool, make_unit_of_work)`; structurally the
    `Engine` surface PgDatabase uses). The DSN is the platform secret `settings.db_dsn_ref` (spec-09 §4), read
    through the SecretsProvider when `start()` or the first unit of work needs it, so a process may start before
    its stack is seeded. A failed read or pool creation is the retryable `internal_error` of an unreachable
    Postgres and is retried at most once a second, with the secret read again. A created pool keeps its DSN: a
    rotated `noema/db-dsn` reaches a running process only through a restart (docs/modules/integration.md §9).

    R4 (Wave E3), with `instruments`: a sampled unit of work (`DATASTORE_SAMPLE_RATE`) observes
    `aghoo_db_pool_acquire_seconds{pool="app"}` (the wait for a pooled connection, connecting included) and
    `aghoo_db_command_seconds{pool="app"}` (acquire to release: its statements and the code between them); `in_use`
    (every unit) and `max_size` feed the pool gauges."""

    def __init__(
        self,
        secrets: SecretsProvider,
        ref: str,
        clock: Clock,
        *,
        max_size: int = DB_POOL_MAX,
        instruments: Instruments | None = None,
    ) -> None:
        self._secrets = secrets
        self._ref = ref
        self._clock = clock
        self._max_size = max_size
        self._instruments = instruments
        self.in_use = 0
        self._engine: Engine | None = None
        self._dsn: SecretStr | None = None
        self._lock = anyio.Lock()
        self._retry_at = 0.0
        self._closed = False

    @property
    def max_size(self) -> int:
        return self._max_size

    async def dsn(self) -> SecretStr:
        """The noema_app DSN (registered with the redactor). Raises the SecretsProvider's errors."""
        if self._dsn is None:
            raw = await self._secrets.get(self._ref)
            self._dsn = SecretStr(raw.decode().strip(), register=True)
        return self._dsn

    async def engine(self) -> Engine:
        if self._engine is not None:
            return self._engine
        async with self._lock:
            if self._engine is None:
                if self._closed or self._clock.monotonic() < self._retry_at:  # never reopened after stop()
                    raise db_unavailable()
                try:
                    settings = app_pool_settings(await self.dsn(), max_size=self._max_size)
                    self._engine = await Engine.create(settings)
                except Exception as exc:
                    self._retry_at = self._clock.monotonic() + DB_RETRY_S
                    self._dsn = None  # re-read the secret next time (a rotated noema/db-dsn, a re-bootstrap)
                    _log.warning("database pool unavailable (%s); retrying", type(exc).__name__)
                    raise db_unavailable() from exc
            return self._engine

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[PooledConnection]:
        engine = await self.engine()
        ins = self._instruments if _sampled() else None
        clock = self._clock
        began = got = clock.monotonic() if ins is not None else 0.0
        async with engine.acquire() as conn:
            self.in_use += 1
            try:
                if ins is not None:
                    got = clock.monotonic()
                    ins.observe("aghoo_db_pool_acquire_seconds", got - began, pool="app")
                yield conn
            finally:
                self.in_use -= 1
                if ins is not None:
                    ins.observe("aghoo_db_command_seconds", clock.monotonic() - got, pool="app")

    async def warm(self) -> None:
        """start(): create the pool now (a failure is logged; the first unit of work retries)."""
        with suppress(NoemaError):
            await self.engine()

    async def close(self) -> None:
        self._closed = True
        engine, self._engine = self._engine, None
        if engine is not None:
            await engine.close()


class _DeferredListener:
    """The `PgListener.listen` of the job workers (`jobs`) and the notify bridge (`noema_events`), whose DSN is the
    `_DbPool`'s: it waits until the DSN is readable (retrying 1..30 s), then LISTENs on its own connection."""

    def __init__(self, pool: _DbPool, clock: Clock) -> None:
        self._pool = pool
        self._clock = clock

    async def listen(self, channel: str, on_notify: Callable[[str], None]) -> None:
        delay = 1.0
        while True:
            try:
                dsn = await self._pool.dsn()
                break
            except Exception as exc:
                _log.warning("LISTEN %s: the database DSN is unreadable (%s)", channel, type(exc).__name__)
                await self._clock.sleep(delay)
                delay = min(delay * 2, LISTEN_BACKOFF_MAX_S)
        await PgListener(dsn, self._clock).listen(channel, on_notify)


class ActiveConfigRegistry:
    """`services.config` (implements `ConfigRegistry`): the active `platform.config_version`, loaded with
    `config.loader` (spec-05 §3 steps 1-6; a `db:<n>` price catalog from `platform.price_catalog`) into an
    `InMemoryConfigRegistry` (current + the previous 2 pinned). Empty until the first load: `current()` then raises
    a retryable `internal_error` and `/readyz` is 503. `refresh(db)` (the watcher: `config_changed` + the 5 s poll)
    never raises; a rejected version (load error, digest mismatch) keeps the current snapshot and is logged once."""

    def __init__(self, env: Env) -> None:
        self._env: Env = env
        self._inner: InMemoryConfigRegistry | None = None
        self._callbacks: list[Callable[[ConfigSnapshot], None]] = []
        self._lock = threading.Lock()
        self._rejected: tuple[int, str] | None = None
        self._missing_logged = False

    def current(self) -> ConfigSnapshot:
        return self._registry().current()

    def get(self, version: int) -> ConfigSnapshot:
        return self._registry().get(version)

    def on_change(self, cb: Callable[[ConfigSnapshot], None]) -> None:
        with self._lock:
            self._callbacks.append(cb)
            if self._inner is not None:
                self._inner.on_change(cb)

    def activate(self, snapshot: ConfigSnapshot) -> bool:
        """Make `snapshot` current (the first one creates the registry and notifies the callbacks)."""
        with self._lock:
            inner = self._inner
            if inner is None:
                inner = InMemoryConfigRegistry(snapshot)
                for cb in self._callbacks:
                    inner.on_change(cb)
                self._inner = inner
                first: tuple[Callable[[ConfigSnapshot], None], ...] | None = tuple(self._callbacks)
            else:
                first = None
        if first is None:
            return inner.activate(snapshot)
        failures: list[Exception] = []
        for cb in first:
            try:
                cb(snapshot)
            except Exception as exc:
                failures.append(exc)
        if failures:
            raise ExceptionGroup("config on_change callbacks failed", failures)
        return True

    def _registry(self) -> InMemoryConfigRegistry:
        inner = self._inner
        if inner is None:
            raise NoemaError(ErrorCode.INTERNAL_ERROR, "No platform config is active yet.")
        return inner

    def _is_current(self, version: int, digest: str) -> bool:
        inner = self._inner
        return inner is not None and (inner.current().version, inner.current().digest) == (version, digest)

    async def refresh(self, db: Database) -> bool:
        """Load the active version unless it is the current snapshot; True iff a new snapshot became current."""
        try:
            async with db.system_uow() as calls:
                row = await calls.active_config()
                if row is None:
                    if not self._missing_logged:
                        self._missing_logged = True
                        _log.warning("no active platform config yet (noema-admin config approve)")
                    return False
                version, digest = int(cast("int", row["version"])), str(row["digest"])
                body = cast("Mapping[str, Any]", row["body"])
                if self._is_current(version, digest) or self._rejected == (version, digest):
                    return False
                price_version = referenced_price_version(body)
                price_row = await calls.price_catalog(price_version) if price_version is not None else None
        except Exception as exc:
            _log.warning("platform config refresh failed (%s); retrying", type(exc).__name__)
            return False
        prices = cast("Mapping[str, Any] | None", price_row["body"] if price_row is not None else None)
        try:
            snapshot = load_config(body, env=self._env, prices=lambda n: prices if n == price_version else None)
            if snapshot.digest != digest:
                raise ConfigError([f"digest {snapshot.digest} differs from the stored {digest}"])
        except Exception as exc:  # a ConfigError, or a loader bug on this body: rejected either way, logged once
            self._rejected = (version, digest)
            detail = exc if isinstance(exc, ConfigError) else type(exc).__name__
            _log.error("platform config v%d rejected; keeping the current one: %s", version, detail)
            return False
        try:
            changed = self.activate(snapshot)
        except ExceptionGroup as group:
            _log.error("config on_change callbacks failed: %d", len(group.exceptions))
            changed = True
        if changed:
            _log.info("platform config v%d is active (%s)", snapshot.version, snapshot.digest)
        return changed


ALL_PROCESSES: Final = frozenset({"api", "jobs", "media-worker"})
API: Final = frozenset({"api"})
JOBS: Final = frozenset({"jobs"})
API_JOBS: Final = frozenset({"api", "jobs"})


@dataclass(frozen=True, slots=True)
class _Step:
    """One lifecycle step of a wired component: run by `start()` in the listed processes (or by `stop()`)."""

    name: str
    processes: frozenset[str]
    run: Callable[[], Awaitable[None]]


@dataclass
class _Lifecycle:
    """Mutable process state behind the frozen `Services`: what the wired components need at `start` (in build
    order), what `start` set up and what `stop` must undo (closes in reverse order)."""

    process: Process | None = None
    started: bool = False
    stopped: bool = False
    shutdown: list[Callable[[], None]] = field(default_factory=list)  # sync callbacks run last (telemetry)
    restore_signal: Callable[[], None] | None = None
    metrics_ports: int = 1  # WEB_CONCURRENCY: this process serves its metrics on the first free one of these
    telemetry_setup: TelemetrySetup | None = None  # the real telemetry's providers (Prometheus mode outside test)
    starts: list[_Step] = field(default_factory=list)  # awaited in order by start()
    tasks: list[_Step] = field(default_factory=list)  # spawned in the root task group by start()
    drains: list[tuple[str, frozenset[str], Callable[[float], Awaitable[None]]]] = field(default_factory=list)
    closes: list[_Step] = field(default_factory=list)  # awaited in reverse order by stop(), every process

    def on_start(self, name: str, processes: frozenset[str], run: Callable[[], Awaitable[None]]) -> None:
        self.starts.append(_Step(name, processes, run))

    def task(self, name: str, processes: frozenset[str], run: Callable[[], Awaitable[None]]) -> None:
        self.tasks.append(_Step(name, processes, run))

    def on_drain(self, name: str, processes: frozenset[str], run: Callable[[float], Awaitable[None]]) -> None:
        self.drains.append((name, processes, run))

    def on_close(self, name: str, run: Callable[[], Awaitable[None]]) -> None:
        self.closes.append(_Step(name, ALL_PROCESSES, run))


@dataclass(frozen=True)
class Services:
    """One per process; built in the app lifespan (spec-01 §3). `redis` is a bytes client (decode_responses=False);
    every Redis key goes through `keys`. `runtime` is the process runtime (`ProcessRuntime`); `wiring` records the
    component kind of each field ("fake" | "real"). Satisfies `noema.core.protocols.services.ServiceSet`."""

    clock: Clock
    ids: IdGen
    config: ConfigRegistry
    secrets: SecretsProvider
    broker: CredentialBroker
    adapters: AdapterRegistry
    health: HealthRegistry
    retry_budget: RetryBudget
    planner: WorkGraphPlanner
    admission: Admission
    estimator: TokenEstimator
    cost: CostMeter
    outbox: Outbox
    db: Database
    jobs: JobQueue
    idempotency: IdempotencyStore
    events: TurnEventLog
    sse: SseWriter
    files: FileService
    views: InputViewResolver
    negotiator: MediaNegotiator
    media_budget: MediaBudgetPlanner
    calibrators: CalibratorRegistry
    sampler: LabelingSampler
    compiler: QuestionCompiler
    decision_cache: DecisionCache
    decisions: DecisionEngine
    generation: GenerationEngine
    authn: Authenticator
    authz: Authorizer
    telemetry: Telemetry
    redis: redis.asyncio.Redis
    keys: RedisKeys
    s3: S3Client
    runtime: TaskGroupRuntime
    settings: ProcessSettings
    wiring: Mapping[str, str]
    _lifecycle: _Lifecycle = field(default_factory=_Lifecycle, repr=False, compare=False)


if TYPE_CHECKING:

    def _services_satisfy_the_core_view(services: Services) -> ServiceSet:
        return services  # mypy: a renamed or retyped field breaks here, not at runtime in a route


_NO_FAKE: Final = frozenset({"redis", "runtime"})
SERVICE_FIELDS: Final[tuple[str, ...]] = tuple(
    f for f in Services.__dataclass_fields__ if f not in ("settings", "wiring", "_lifecycle")
)
type AdapterWrapper = Callable[[AdapterRegistry], AdapterRegistry]


@dataclass
class _Wiring:
    """The fields chosen so far (real or fake: `v`), the faked names and the process settings, while `_wire_real`
    fills the remaining fields in dependency order."""

    v: dict[str, Any]
    fake: frozenset[str]
    settings: ProcessSettings
    lc: _Lifecycle
    pool: _DbPool | None = None

    def real(self, name: str) -> bool:
        return name not in self.fake

    def partial_services(self) -> ServiceSet:
        """The fields built so far as a `ServiceSet` view, for factories that take one (`from_services`)."""
        return cast("ServiceSet", SimpleNamespace(**self.v, settings=self.settings))


def _wire_platform(w: _Wiring) -> None:
    """config, secrets, s3, db (+ the config watcher), outbox, jobs, estimator, cost, idempotency (docs/modules
    ops-ledger.md, identity.md)."""
    from noema.db.repos import make_unit_of_work
    from noema.db.uow import PgDatabase
    from noema.identity.secrets import SecretsManagerProvider
    from noema.ops.cost import HeuristicTokenEstimator, LedgerCostMeter
    from noema.ops.idempotency import BodyStore, PgIdempotencyStore
    from noema.ops.jobs import PgJobQueue
    from noema.ops.outbox import OutboxDrainer, WriteBehindOutbox

    v, s, lc = w.v, w.settings, w.lc
    clock, ids, keys, redis, telemetry = v["clock"], v["ids"], v["keys"], v["redis"], v["telemetry"]
    instruments, ov = telemetry.instruments, s.endpoint_overrides
    if w.real("config"):
        v["config"] = ActiveConfigRegistry(s.env)
    if w.real("secrets"):
        secrets = SecretsManagerProvider(clock, region=PLATFORM_REGION, endpoint_url=ov.get("secretsmanager"))
        v["secrets"] = secrets
        lc.on_start("identity.secrets", ALL_PROCESSES, secrets.start)
        lc.on_close("identity.secrets", secrets.aclose)
    if w.real("s3"):
        s3 = _s3_client(s)
        v["s3"] = s3
        lc.on_start("s3", ALL_PROCESSES, s3.open)
    if w.real("db"):
        w.pool = pool = _DbPool(v["secrets"], s.db_dsn_ref, clock, instruments=instruments)
        v["db"] = PgDatabase(cast("Engine", pool), make_unit_of_work)
        lc.on_start("db.pool", ALL_PROCESSES, pool.warm)
        lc.on_close("db.pool", pool.close)
        lc.task("telemetry.pool_gauges", ALL_PROCESSES, lambda: _pool_gauges(pool, redis, instruments, clock))
    config, db = v["config"], v["db"]
    if isinstance(config, ActiveConfigRegistry):
        lc.on_start("config.load", ALL_PROCESSES, lambda: _ignore(config.refresh(db)))
        lc.task("config.watch", ALL_PROCESSES, lambda: _config_watch(config, db, redis, keys.config_changed(), clock))
    if w.real("outbox"):
        outbox = WriteBehindOutbox(db, redis, keys, clock, instruments)
        drainer = OutboxDrainer(db, redis, keys, clock, instruments, consumer=s.pod_id)
        v["outbox"] = outbox
        lc.task("ops.outbox.writer", ALL_PROCESSES, outbox.run)
        lc.task("ops.outbox.drainer", API_JOBS, drainer.run)
    if w.real("jobs"):
        listener = cast("PgListener", _DeferredListener(w.pool, clock)) if w.pool is not None else None
        v["jobs"] = PgJobQueue(db, clock, ids, pod_id=s.pod_id, listener=listener, redact=telemetry.redact)
    if w.real("estimator"):
        v["estimator"] = HeuristicTokenEstimator(table=_estimator_override(s))
    if w.real("cost"):
        v["cost"] = LedgerCostMeter(clock, config, v["outbox"], instruments)
    if w.real("idempotency"):
        bodies = BodyStore(v["s3"], bucket=s.s3_bucket, env=s.env)
        v["idempotency"] = PgIdempotencyStore(db, redis, keys, bodies, clock, ids, config, instruments, pod_id=s.pod_id)


async def _ignore(step: Awaitable[object]) -> None:
    await step


def _estimator_override(settings: ProcessSettings) -> Mapping[str, Any] | None:
    """The TokenEstimator table of `NOEMA_TOKEN_ESTIMATOR_TABLE` (a Phase 0 refit, `estimator_fit`'s JSON; it
    replaces `TOKEN_ESTIMATOR_V1`, an alias not in it counts chars/4), None when unset; an unreadable or invalid file
    stops the process (`StartupError`)."""
    if settings.token_estimator_table is None:
        return None
    from pathlib import Path

    from noema.ops.cost.estimator import load_table

    try:
        return load_table(Path(settings.token_estimator_table))
    except (OSError, ValueError) as exc:
        raise StartupError(f"NOEMA_TOKEN_ESTIMATOR_TABLE: {exc}") from None


def _wire_models(w: _Wiring, *, broker_kind: str, wrap_adapters: AdapterWrapper | None, ttft_rule: bool) -> Any:
    """retry_budget (ONE instance, also the adapters'), health, planner, admission, broker, adapters (+ the wrap
    hook, before any engine captures the registry), events, sse (docs/modules adapters.md, ops_admission.md,
    identity.md, generation.md). Returns identity's `SecurityEvents` (the adapters' and the revocation sink)."""
    from noema.adapters import (
        AdapterRegistryImpl,
        BreakerPolicy,
        BreakerRegistry,
        ConverseAdapter,
        MantleAdapter,
        SlidingRetryBudget,
    )
    from noema.identity.ambient import AmbientBroker
    from noema.identity.security import SecurityEvents
    from noema.identity.sts import StsBroker
    from noema.identity.sts_aws import AioStsClient
    from noema.ops.admission import DefaultWorkGraphPlanner, GcraAdmission
    from noema.streaming import BackpressureSseWriter, InProcessTurnEventLog, RedisEventMirror

    v, s, lc = w.v, w.settings, w.lc
    clock, ids, keys, redis, telemetry = v["clock"], v["ids"], v["keys"], v["redis"], v["telemetry"]
    config = v["config"]
    ov = s.endpoint_overrides
    if w.real("retry_budget"):
        budget = SlidingRetryBudget(clock, keys=keys, pod_id=s.pod_id)
        v["retry_budget"] = budget
        lc.task("adapters.retry_budget_sync", API_JOBS, lambda: budget.run(redis))
    if w.real("health"):
        policy = None if ttft_rule else BreakerPolicy(ttft_factor=math.inf)  # NOEMA_BREAKER_TTFT_RULE=off (dev/test)
        health = BreakerRegistry(clock, policy=policy, telemetry=telemetry, keys=keys)
        v["health"] = health
        lc.task("adapters.breaker_sync", API_JOBS, lambda: health.run(redis))
    if w.real("planner"):
        v["planner"] = DefaultWorkGraphPlanner()
    if w.real("admission"):
        v["admission"] = GcraAdmission(
            clock, config, keys, redis, env=s.env, instruments=telemetry.instruments, expected_pods=s.expected_pods
        )
    security = SecurityEvents(telemetry=telemetry, outbox=v["outbox"], ids=ids, clock=clock)
    if w.real("broker"):
        if broker_kind == "ambient":
            ambient = AmbientBroker(
                config=config, clock=clock, env=s.env, region=PLATFORM_REGION, endpoint_url=ov.get("sts"),
                security=security,
            )  # fmt: skip
            v["broker"] = ambient
            lc.on_start("identity.broker", API_JOBS, ambient.start)
            lc.on_close("identity.broker", ambient.aclose)
        else:
            sts = AioStsClient(region=PLATFORM_REGION, endpoint_url=ov.get("sts"))  # pod credentials
            broker = StsBroker(
                config=config, secrets=v["secrets"], sts=sts, clock=clock, runtime=v["runtime"], deployment=s.env,
                pod_id=s.pod_id, telemetry=telemetry, security=security,
            )  # fmt: skip
            v["broker"] = broker
            lc.on_start("identity.sts", API_JOBS, sts.start)
            lc.on_start("identity.broker", API_JOBS, broker.start)
            lc.on_close("identity.sts", sts.aclose)
            lc.on_close("identity.broker", broker.aclose)
    if w.real("adapters"):
        common: dict[str, Any] = {
            "config": config, "broker": v["broker"], "clock": clock, "retry_budget": v["retry_budget"],
            "telemetry": telemetry, "security": security, "endpoint_overrides": ov,
        }  # fmt: skip
        converse, mantle = ConverseAdapter(**common), MantleAdapter(**common)
        v["adapters"] = AdapterRegistryImpl(config, {"bedrock-converse": converse, "bedrock-mantle-chat": mantle})
        for name, adapter in (("converse", converse), ("mantle", mantle)):
            lc.on_start(f"adapters.{name}.prewarm", API_JOBS, adapter.prewarm)
            lc.on_close(f"adapters.{name}", adapter.aclose)
        lc.on_start("adapters.probe_check", API_JOBS, lambda: _log_probe_discrepancies(config))
    if wrap_adapters is not None:
        v["adapters"] = wrap_adapters(v["adapters"])
    if w.real("events"):
        mirror = RedisEventMirror(redis, keys, clock)
        events = InProcessTurnEventLog(clock, mirror=mirror)
        v["events"] = events
        lc.task("streaming.event_log", API, events.run)
        lc.on_drain("streaming.mirror", API, lambda grace_s: mirror.drain())
    if w.real("sse"):
        v["sse"] = BackpressureSseWriter(clock)
    return security


async def _log_probe_discrepancies(config: ConfigRegistry) -> None:
    """start(): every config claim the Bedrock probes refuted, at WARNING (docs/modules/adapters.md)."""
    from noema.adapters import probe_discrepancies

    try:
        snapshot = config.current()
    except NoemaError:
        return  # no active config yet
    for problem in probe_discrepancies(snapshot):
        _log.warning("model capability probe discrepancy: %s", problem)


def _wire_domains(w: _Wiring, security: Any) -> None:
    """files, views, negotiator, media_budget, calibrators, sampler, compiler, decision_cache, decisions,
    generation, authn, authz, and the jobs-process loops (docs/modules media_pipeline.md, media_negotiation.md,
    labeling.md, decision.md, generation.md, identity.md, ops-ledger.md)."""
    from noema.decision import DecisionCacheImpl, DecisionEngineImpl, EngineDeps, QuestionCompilerImpl
    from noema.generation import StreamingGenerationEngine
    from noema.identity.authn import ApiKeyAuthenticator
    from noema.identity.authz import MatrixAuthorizer
    from noema.identity.revocation import run_revocation_listener
    from noema.labeling import DbCalibratorRegistry, Sampler
    from noema.media.budget import MediaBudgetPlannerImpl
    from noema.media.negotiation import MediaNegotiatorImpl
    from noema.media.pipeline import MediaFileService
    from noema.media.views import InputViewResolverImpl
    from noema.ops.admission import GcraRateLimiter
    from noema.ops.jobs import PeriodicScheduler
    from noema.ops.notify_bridge import NotifyBridge
    from noema.ops.reapers import JobReaper

    v, s, lc = w.v, w.settings, w.lc
    clock, ids, keys, redis, telemetry = v["clock"], v["ids"], v["keys"], v["redis"], v["telemetry"]
    config = v["config"]
    if w.real("files"):
        v["files"] = MediaFileService.from_services(w.partial_services())  # db, jobs, s3, redis, keys, outbox, ...
    if w.real("views"):
        v["views"] = InputViewResolverImpl(v["files"])
    if w.real("negotiator"):
        v["negotiator"] = MediaNegotiatorImpl(v["files"])
    if w.real("media_budget"):
        v["media_budget"] = MediaBudgetPlannerImpl()
    if w.real("calibrators"):
        registry = DbCalibratorRegistry(v["db"])  # the concrete instance: the Protocol has no listen()
        v["calibrators"] = registry
        channel = keys.calibrators_changed()
        lc.task("labeling.calibrators_listen", API_JOBS, lambda: registry.listen(redis, channel, clock=clock))
    if w.real("sampler"):
        v["sampler"] = Sampler(clock, config, v["outbox"], expected_pods=s.expected_pods)
    if w.real("compiler"):
        v["compiler"] = QuestionCompilerImpl(config)
    if w.real("decision_cache"):
        v["decision_cache"] = DecisionCacheImpl(redis, keys, clock, telemetry.instruments)
    if w.real("decisions"):
        deps = EngineDeps(
            clock=clock, ids=ids, adapters=v["adapters"], broker=v["broker"], secrets=v["secrets"],
            views=v["views"], health=v["health"], estimator=v["estimator"], cost=v["cost"],
            calibrators=v["calibrators"], sampler=v["sampler"], cache=v["decision_cache"], keys=keys, db=v["db"],
            outbox=v["outbox"], telemetry=telemetry,
        )  # fmt: skip
        v["decisions"] = DecisionEngineImpl(deps)
    if w.real("generation"):
        v["generation"] = StreamingGenerationEngine(
            clock=clock, ids=ids, adapters=v["adapters"], broker=v["broker"], health=v["health"],
            retry_budget=v["retry_budget"], estimator=v["estimator"], cost=v["cost"], telemetry=telemetry,
        )  # fmt: skip
    if w.real("authn"):
        limiter = GcraRateLimiter(clock, keys, redis, expected_pods=s.expected_pods)  # authfail:<ip> (spec-09 §2)
        authn = ApiKeyAuthenticator(
            db=v["db"], secrets=v["secrets"], limiter=limiter, clock=clock, runtime=v["runtime"], env=s.env
        )
        v["authn"] = authn
        lc.on_start("identity.authn", API, authn.start)
        lc.task("identity.keys_revoked", API, lambda: run_revocation_listener(redis, keys, authn, security, clock))
    if w.real("authz"):
        v["authz"] = MatrixAuthorizer()
    if w.pool is not None:  # the jobs process's ledger loops (spec-11 §4, §6)
        scheduler, reaper = PeriodicScheduler(v["db"], clock), JobReaper(v["db"], clock)
        bridge = NotifyBridge(cast("PgListener", _DeferredListener(w.pool, clock)), redis, keys)
        lc.task("ops.jobs.scheduler", JOBS, scheduler.run)
        lc.task("ops.reapers.jobs", JOBS, reaper.run)
        lc.task("ops.notify_bridge", JOBS, bridge.run)


def _namespace(settings: ProcessSettings) -> str:
    if settings.env == "test":
        return f"ns:{settings.agent_id or 'local'}:{os.getpid()}:{uuid.uuid4().hex[:8]}:"
    return "noema:"


def otlp_traces_endpoint(environ: Mapping[str, str]) -> str | None:
    """The OTLP/HTTP traces URL from the standard variables: `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, else
    `OTEL_EXPORTER_OTLP_ENDPOINT` + `/v1/traces`; None (both unset) = no exporter, spans are not recorded (Wave E:
    before, no process could export traces at all)."""
    traces = (environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or "").strip()
    if traces:
        return traces
    base = (environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or "").strip()
    return f"{base.rstrip('/')}/v1/traces" if base else None


@dataclass(frozen=True, slots=True)
class _RedisMetrics:
    """R4 (Wave E3): where the metered Redis client and pool observe (`_metered_redis`)."""

    instruments: Instruments
    clock: Clock

    def observe(self, name: str, began: float, **labels: str) -> None:
        self.instruments.observe(name, self.clock.monotonic() - began, **labels)


def _redis_client(settings: ProcessSettings, metrics: _RedisMetrics | None = None) -> redis.asyncio.Redis:
    """Connects lazily. One `BlockingConnectionPool` of `settings.redis_max_connections` connections, pub/sub ones
    included (Wave E3): a burst waits up to `REDIS_POOL_TIMEOUT_S` for a free connection, then that call fails with
    the ConnectionError of an unreachable Redis (every caller is fail-soft), instead of the unbounded pool's
    `MaxConnectionsError` under overload (C2b). TCP keepalives (30 s idle, then 3 probes 10 s apart; Wave E): a
    pub/sub connection only reads, so without them a peer that died without FIN/RST would look idle for the life of
    the pod. With `metrics` (R4) the client observes `aghoo_redis_command_seconds{command}` for a sampled command or
    pipeline (`DATASTORE_SAMPLE_RATE`; the wait for a connection included) and the pool
    `aghoo_redis_pool_acquire_seconds` for a sampled acquire."""
    import socket

    pool_class, client_class = _metered_redis()
    keepalive = {socket.TCP_KEEPIDLE: 30, socket.TCP_KEEPINTVL: 10, socket.TCP_KEEPCNT: 3}
    pool = pool_class.from_url(
        settings.redis_url,
        max_connections=settings.redis_max_connections,
        timeout=REDIS_POOL_TIMEOUT_S,
        decode_responses=False,
        socket_keepalive=True,
        socket_keepalive_options=keepalive,
    )
    pool.metrics = metrics
    client = client_class.from_pool(pool)
    client.metrics = metrics
    return cast("redis.asyncio.Redis", client)


@functools.cache
def _metered_redis() -> tuple[Any, Any]:
    """The pool and client classes of `_redis_client` (built on first use: importing composition imports no redis).
    Their `metrics` None (e.g. on `Redis.client()` copies) observes nothing; behavior is redis-py's otherwise."""
    import redis.asyncio
    from redis.asyncio.client import Pipeline
    from redis.exceptions import MaxConnectionsError

    class MeteredPool(redis.asyncio.BlockingConnectionPool):
        """A free (or still creatable) connection is taken without the blocking pool's Condition and timer, which
        cost ~9 us of CPU per command (reports/qa/e3/misc-fixes.md); only an exhausted pool waits, at most
        `timeout`. A caller that loses the last connection to another between the check and the take waits too."""

        metrics: _RedisMetrics | None = None

        async def get_connection(self, *args: Any, **kwargs: Any) -> Any:
            m = self.metrics
            began = m.clock.monotonic() if m is not None and _sampled() else None
            try:
                if self.can_get_connection():
                    with suppress(MaxConnectionsError):
                        return await redis.asyncio.ConnectionPool.get_connection(self)  # type: ignore[no-untyped-call]
                return await super().get_connection(*args, **kwargs)  # type: ignore[no-untyped-call]
            finally:
                if m is not None and began is not None:
                    m.observe("aghoo_redis_pool_acquire_seconds", began)

    class MeteredPipeline(Pipeline):
        metrics: _RedisMetrics | None = None

        async def execute(self, raise_on_error: bool = True) -> list[Any]:
            m = self.metrics
            if m is None or not _sampled():
                return await super().execute(raise_on_error)
            began = m.clock.monotonic()
            try:
                return await super().execute(raise_on_error)
            finally:
                m.observe("aghoo_redis_command_seconds", began, command="MULTI" if self.is_transaction else "PIPELINE")

    class MeteredRedis(redis.asyncio.Redis):
        metrics: _RedisMetrics | None = None

        async def execute_command(self, *args: Any, **options: Any) -> Any:
            m = self.metrics
            if m is None or not _sampled():
                return await super().execute_command(*args, **options)  # type: ignore[no-untyped-call]
            began = m.clock.monotonic()
            try:
                return await super().execute_command(*args, **options)  # type: ignore[no-untyped-call]
            finally:
                m.observe("aghoo_redis_command_seconds", began, command=str(args[0]))

        def pipeline(self, transaction: bool = True, shard_hint: str | None = None) -> Pipeline:
            pipe = MeteredPipeline(self.connection_pool, self.response_callbacks, transaction, shard_hint)
            pipe.metrics = self.metrics
            return pipe

    return MeteredPool, MeteredRedis


def _fakes_module(name: str) -> Any:
    try:
        return importlib.import_module(f"tests.fakes.{name}")
    except ImportError as exc:  # fakes live in the repository's tests package (dev/test only; not in the images)
        raise StartupError("fakes= needs the repository's tests package: run from the repository root") from exc


def _dev_principal(settings: ProcessSettings, config: ConfigRegistry) -> tuple[str, Principal] | None:
    """The NOEMA_DEV_API_KEY token and its principal (dev/test only), or None."""
    if settings.dev_api_key is None:
        return None
    token = settings.dev_api_key.get_secret_value()
    match = API_KEY_FORMAT.fullmatch(token)
    assert match is not None  # validated by process_settings
    if not isinstance(config, ActiveConfigRegistry):  # the real registry loads its config at start()
        try:
            config.current().tenant(DEV_TENANT_ID)
        except NoemaError:
            raise StartupError(f"NOEMA_DEV_API_KEY needs tenant {DEV_TENANT_ID!r} in the active config") from None
    principal = Principal(
        tenant_id=DEV_TENANT_ID,
        project_id=DEV_PROJECT_ID,
        principal_id=DEV_PRINCIPAL_ID,
        key_id=match.group(1),
        roles=frozenset({"developer", "tenant_admin"}),
        scopes=ALL_SCOPES,
        request_id="",
    )
    return token, principal


def build(
    env: Env,
    *,
    fakes: frozenset[str] = frozenset(),
    environ: Mapping[str, str] | None = None,
    wrap_adapters: AdapterWrapper | None = None,
    process: Process = "api",
) -> Services:
    """Build the process's `Services` (spec-01 §3): the real implementation of every field, except the fields named
    in `fakes`, which get their `tests/fakes` implementation (dev/test only). `wrap_adapters` wraps the adapter
    registry (real or fake) before the engines capture it. `process` names the telemetry resource (`service.name`
    `noema-<process>`; the app lifespan builds the api's). Opens no connection (see the module docstring).

    Raises `StartupError` (invalid settings or `fakes` names) and `ForbiddenInEnvironment` (a disallowed
    component or setting for `env`, e.g. any fake, endpoint overrides or AmbientBroker outside dev/test)."""
    environ = os.environ if environ is None else environ
    settings = process_settings(env, environ)
    if process not in PROCESSES:
        raise StartupError(f"unknown process {process!r}")
    names = frozenset(fakes)
    if unknown := names - set(SERVICE_FIELDS):
        raise StartupError(f"unknown Services fields in fakes: {sorted(unknown)}")
    if no_fake := names & _NO_FAKE:
        raise StartupError(f"no fake exists for: {sorted(no_fake)}")
    wiring = {n: "fake" if n in names else "real" for n in SERVICE_FIELDS}
    for kind in sorted(set(wiring.values())):
        check_component(kind, env)
    broker = broker_kind(environ, env) if "broker" not in names else "fake"
    ttft_rule = ttft_rule_armed(environ, env)

    clock: Clock = _fakes_module("clock").FakeClock() if "clock" in names else SystemClock()
    lc = _Lifecycle(metrics_ports=api_workers(environ))
    telemetry: Telemetry | None = None
    if "telemetry" not in names:
        from noema.telemetry import OtelTelemetry, setup_telemetry

        source = "ambient" if broker == "ambient" else "sts"
        lc.telemetry_setup = setup = setup_telemetry(
            service_name=f"noema-{process}", env=env, memory=env == "test", otlp_endpoint=otlp_traces_endpoint(environ)
        )
        telemetry = OtelTelemetry(setup, env=env, static_attributes={"aghoo.credentials.source": source})
        lc.shutdown.append(setup.shutdown)
    keys = RedisKeys(_namespace(settings))
    values: dict[str, Any] = {}
    if names:
        world = _fakes_module("world").build_world(clock=clock, telemetry=telemetry, env=env, namespace=keys.ns)
        values = {n: getattr(world, n) for n in names}
        telemetry = telemetry or world.telemetry
    assert telemetry is not None
    redis_metrics = _RedisMetrics(telemetry.instruments, clock)
    values.update(clock=clock, keys=keys, telemetry=telemetry, redis=_redis_client(settings, redis_metrics))
    values["runtime"] = TaskGroupRuntime(clock)
    if "ids" not in names:
        values["ids"] = UlidIdGen(clock)
    w = _Wiring(values, names, settings, lc)
    _wire_platform(w)
    security = _wire_models(w, broker_kind=broker, wrap_adapters=wrap_adapters, ttft_rule=ttft_rule)
    _wire_domains(w, security)
    dev = _dev_principal(settings, values["config"]) if wiring["authn"] == "fake" else None
    if dev is not None:
        values["authn"].register(*dev)
    elif settings.dev_api_key is not None:
        _log.warning("NOEMA_DEV_API_KEY is ignored by the real authenticator (mint keys with noema-admin)")
    return Services(**values, settings=settings, wiring=wiring, _lifecycle=lc)


async def _calibrator_poll(services: Services) -> None:
    """The 5 s calibrator registry refresh (spec-02 §9); `refresh()` never raises by contract."""
    while True:
        await services.calibrators.refresh()
        await services.clock.sleep(CALIBRATOR_POLL_S)


async def _loop_lag(services: Services) -> None:
    """`aghoo_event_loop_lag_seconds` (spec-10 §5, the 08 §9 scaling signal), every process: how late a
    `LOOP_LAG_EVERY_S` sleep wakes up on the process's event loop."""
    clock, instruments = services.clock, services.telemetry.instruments
    while True:
        begin = clock.monotonic()
        await clock.sleep(LOOP_LAG_EVERY_S)
        instruments.set("aghoo_event_loop_lag_seconds", max(0.0, clock.monotonic() - begin - LOOP_LAG_EVERY_S))


async def _pool_gauges(db: _DbPool, redis: redis.asyncio.Redis, instruments: Instruments, clock: Clock) -> None:
    """R4 (Wave E3), every process with the real database: every `POOL_GAUGES_EVERY_S` the Postgres pool's
    `aghoo_db_pool_size{pool="app"}` (its max_size) and `aghoo_db_pool_in_use`, and the Redis pool's
    `aghoo_redis_pool_size` (max_connections) and `aghoo_redis_pool_in_use` (checked out, pub/sub included)."""
    pool = redis.connection_pool
    while True:
        instruments.set("aghoo_db_pool_size", db.max_size, pool="app")
        instruments.set("aghoo_db_pool_in_use", db.in_use, pool="app")
        instruments.set("aghoo_redis_pool_size", pool.max_connections)
        instruments.set("aghoo_redis_pool_in_use", pool.get_connection_count()[1][0])
        await clock.sleep(POOL_GAUGES_EVERY_S)


def supervised(name: str, run: Callable[[], Awaitable[None]], clock: Clock) -> Callable[[], Awaitable[None]]:
    """A background loop that restarts after an exception (logged at ERROR; backoff 1..30 s, reset after a run of
    at least 30 s), so a bug in one iteration does not stop the loop for the life of the pod while `/readyz` stays
    200. Returning normally ends it; cancellation is never caught."""

    async def supervise() -> None:
        delay = 1.0
        while True:
            began = clock.monotonic()
            try:
                await run()
                return
            except Exception as exc:
                if clock.monotonic() - began >= LISTEN_BACKOFF_MAX_S:
                    delay = 1.0
                _log.error("background task %s failed (%s); restarting in %.0f s", name, type(exc).__name__, delay,
                           exc_info=exc)  # fmt: skip
            await clock.sleep(delay)
            delay = min(delay * 2, LISTEN_BACKOFF_MAX_S)

    return supervise


async def _config_watch(
    config: ActiveConfigRegistry, db: Database, redis: redis.asyncio.Redis, channel: str, clock: Clock
) -> None:
    """The config watcher (spec-05 §3, every process): reload on `config_changed` (the notify bridge publishes
    approvals and rollbacks) and poll the active version every 5 s (Redis down, missed messages). A subscription
    silent for 15 s despite PINGs is resubscribed (`noema.core.pubsub.heartbeat_messages`)."""
    from noema.ops.jobs import Wakeup

    wake = Wakeup()

    async def subscribe() -> None:
        delay = 1.0
        while True:
            try:
                pubsub = redis.pubsub()
                try:
                    await pubsub.subscribe(channel)
                    delay = 1.0
                    wake.set()  # changes published while unsubscribed
                    async for _ in heartbeat_messages(pubsub, clock):
                        wake.set()
                finally:
                    with anyio.CancelScope(shield=True):
                        await pubsub.aclose()  # type: ignore[no-untyped-call]
            except Exception as exc:
                _log.warning("config_changed subscription lost (%s)", type(exc).__name__)
                await clock.sleep(delay)
                delay = min(delay * 2.0, LISTEN_BACKOFF_MAX_S)

    async with anyio.create_task_group() as tg:
        tg.start_soon(subscribe)
        while True:
            await wake.wait(clock, CONFIG_POLL_S)
            await config.refresh(db)


def _chain_sigterm(services: Services) -> Callable[[], None] | None:
    """api process: SIGTERM starts draining at once and runs the previous handler (uvicorn's) `drain_delay_s`
    later on the event loop. Returns the restore function; None off the main thread (no signal handlers there)."""
    if threading.current_thread() is not threading.main_thread():
        return None
    import asyncio

    loop = asyncio.get_running_loop()
    previous = signal.getsignal(signal.SIGTERM)
    delay = services.settings.drain_delay_s

    def forward(signum: int, frame: FrameType | None) -> None:
        if callable(previous):
            previous(signum, frame)
        elif previous == signal.SIG_DFL:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            signal.raise_signal(signal.SIGTERM)

    draining: set[asyncio.Task[None]] = set()

    async def begin_drain() -> None:
        services.runtime.begin_drain()

    def start_drain() -> None:
        # inside a task: begin_drain starts the hooks with anyio, which needs a current task (sniffio) to find the
        # backend; a plain loop callback has none, so the hooks would never start (NoEventLoopError)
        task = loop.create_task(begin_drain())
        draining.add(task)
        task.add_done_callback(draining.discard)

    def on_sigterm(signum: int, frame: FrameType | None) -> None:
        loop.call_soon_threadsafe(start_drain)
        loop.call_soon_threadsafe(loop.call_later, delay, forward, signum, frame)

    signal.signal(signal.SIGTERM, on_sigterm)

    def restore() -> None:
        if signal.getsignal(signal.SIGTERM) is on_sigterm:
            signal.signal(signal.SIGTERM, previous)

    return restore


def _install_logging() -> Callable[[], None]:
    """Outside test: one JSON stderr handler with the redacting filter on the root logger, so every `noema.*`
    logger (api access/errors/idempotency/validation, runtime, telemetry) is redacted (spec-09 §4). Returns the
    function that restores the previous root handlers (test runs keep pytest's capture handlers)."""
    from noema.telemetry import configure_logging, default_redactor

    root = logging.getLogger()
    previous, level = list(root.handlers), root.level
    handler = configure_logging(default_redactor())
    quiet = {name: logging.getLogger(name).level for name in QUIET_LOGGERS}
    for name in QUIET_LOGGERS:  # one INFO line per Mantle request: too much at 200 streams per pod
        logging.getLogger(name).setLevel(logging.WARNING)

    def restore() -> None:
        root.removeHandler(handler)
        for old in previous:
            root.addHandler(old)
        root.setLevel(level)
        for name, old_level in quiet.items():
            logging.getLogger(name).setLevel(old_level)

    return restore


async def start(services: Services, process: Process) -> None:
    """Start the process (spec-01 §3) in its root task group; await `start` and `stop` from the same task (the app
    lifespan, the worker entrypoints). In order: the redacted JSON log handler (real telemetry outside test), the
    wired components' start steps for `process` (clients, the DB pool, the first config load, the broker, the
    authenticator, the adapters' prewarm), their background tasks and drain hooks, the calibrator poll (api, jobs),
    the turn service's listeners (api), the Prometheus listener on `NOEMA_METRICS_PORT`, the SIGTERM drain chain
    (api). The job workers are the entrypoints' (`noema.entrypoints.jobs`, `.media_worker`). A failing start is
    undone with `stop()`. RuntimeError when called twice."""
    if process not in PROCESSES:
        raise ValueError(f"unknown process {process!r}")
    lc = services._lifecycle
    if lc.started or lc.stopped:
        raise RuntimeError("services were already started")
    lc.started, lc.process = True, process
    runtime = services.runtime
    await runtime.open()
    try:
        setup = lc.telemetry_setup
        if setup is not None and services.settings.env != "test":
            lc.shutdown.append(_install_logging())  # first: the start steps' own logs are redacted JSON too
        for step in lc.starts:
            if process in step.processes:
                await step.run()
        clock = services.clock
        for step in lc.tasks:
            if process in step.processes:
                runtime.spawn(step.name, supervised(step.name, step.run, clock))
        for name, processes, hook in lc.drains:
            if process in processes:
                runtime.on_drain(name, hook)
        if process in ("api", "jobs"):
            runtime.spawn("calibrator-poll", supervised("calibrator-poll", lambda: _calibrator_poll(services), clock))
        runtime.spawn("telemetry.loop_lag", supervised("telemetry.loop_lag", lambda: _loop_lag(services), clock))
        if process == "api":
            from noema.turns.service import TurnService

            TurnService.of(services).start_background()  # cancel listener, owner refresh, watchdog, turns.drain
        port = services.settings.metrics_port
        if port is not None and setup is not None and setup.prometheus is not None:
            lc.shutdown.insert(0, serve_metrics(setup, port, lc.metrics_ports))
        if process == "api":
            lc.restore_signal = _chain_sigterm(services)
        if services.settings.env != "test":
            _freeze_startup_heap()
    except BaseException:
        await stop(services)  # shields its own steps; must run outside any scope entered after runtime.open()
        raise


def serve_metrics(setup: TelemetrySetup, port: int, ports: int) -> Callable[[], None]:
    """Serve the Prometheus exposition on the first free port of `port` .. `port + ports - 1` (the workers of one
    pod take one each); returns the stop function. OSError when every port is taken."""
    for offset in range(ports):
        try:
            bound, stop_metrics = setup.serve_prometheus(port + offset)
        except OSError:
            if offset == ports - 1:
                raise
            continue
        if ports > 1:
            _log.info("metrics on port %d", bound)
        return stop_metrics
    raise AssertionError("unreachable: ports >= 1")


def _freeze_startup_heap() -> None:
    """Collect the startup garbage once, then move every surviving object (modules, botocore service models, schema
    validators, the config) to the permanent generation, so later full collections scan only the request-time heap.
    Without it a full collection of the api took 110-125 ms (each one a loop stall: TTFT p95 +200 ms at 50
    streams, reports/perf/c2b-perf-smoke.md). Not in test processes: they build many short-lived Services, which
    must stay collectable.

    Then, unless the thresholds were already changed (the load harness's experiments), a full collection waits for 100
    young-generation collections instead of 10 (`GC_THRESHOLDS`). A full collection scans the live heap plus the
    cyclic garbage waiting for it; since E3 a turn leaves almost none (~27 objects, was ~430: the decision run's state
    and the deadline watchers' tracebacks), so rarer collections are no longer longer ones: at 200 streams per pod
    (2 processes) 40 -> 100 made them every 21 s instead of 8.6 s at about the same pause (p50 57 -> 62 ms, p99 81 ->
    83 ms) and took the TTFT overhead p95 / p99 from 25.8 / 68.7 to 23.7 / 48.8 ms, RSS +0.5%
    (reports/perf/e3-perf-gc-objects.md section 5). Sprint B chose 40 when the garbage made each pause grow with the
    interval (e-sprint-b.md section 6)."""
    gc.collect()
    gc.freeze()
    if gc.get_threshold() == PYTHON_GC_THRESHOLDS:
        gc.set_threshold(*GC_THRESHOLDS)


async def stop(services: Services) -> None:
    """Graceful shutdown (spec-11 §7 steps 3-5): drain (the runtime's drain hooks: api grace + 10 s, so the running
    turns finalized; jobs and media-worker 10 s for the running handlers), flush the outbox, cancel the background
    tasks, close the components in reverse build order (adapters, broker, STS, the DB pool, secrets), the Redis and
    S3 clients and the telemetry providers. Idempotent; shielded from the caller's cancellation."""
    lc = services._lifecycle
    if lc.stopped:
        return
    lc.stopped = True
    if lc.restore_signal is not None:
        lc.restore_signal()
    with anyio.CancelScope(shield=True):
        if services.runtime.is_open:
            limit = DRAIN_GRACE_S + API_DRAIN_SLACK_S if lc.process == "api" else WORKER_DRAIN_S
            await services.runtime.drain(timeout_s=limit)
        await services.outbox.flush(OUTBOX_FLUSH_S)
    await services.runtime.close()  # leaves the root task group entered by start() (not inside another scope)
    with anyio.CancelScope(shield=True):
        for step in reversed(lc.closes):
            try:
                await step.run()
            except Exception as exc:
                _log.warning("closing %s failed (%s)", step.name, type(exc).__name__)
        await services.redis.aclose()
        await services.s3.close()
    for callback in lc.shutdown:
        callback()
