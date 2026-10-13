"""Suite kits: what a registered factory yields for each conformance suite (see `registry.py`).

A kit bundles the implementation under test with the hooks the suite needs to drive it. Required fields must be
set; hooks typed `... | None` are optional (tests needing them are skipped). Time: every kit carries the
harness `FakeClock`; implementations on the `Clock` protocol should be built on it so tests run in virtual time.
`NullReservation` and `principal()` are shared helpers.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tests.fakes.clock import FakeClock
from tests.fakes.ops_admission import CountingReservation

from noema.core.clock import Clock
from noema.core.ids import IdGen
from noema.core.principal import ALL_SCOPES, Principal, Role, Scope, TenantRef
from noema.core.protocols.adapters import AdapterRegistry
from noema.core.protocols.auth import Authenticator, Authorizer
from noema.core.protocols.config import ConfigRegistry, ConfigSnapshot
from noema.core.protocols.credentials import CredentialBroker, SecretsProvider
from noema.core.protocols.decision import DecisionCache, DecisionEngine, QuestionCompiler
from noema.core.protocols.events import SseWriter, TurnEventLog
from noema.core.protocols.generation import GenerationEngine
from noema.core.protocols.labeling import Calibrator, CalibratorRegistry, LabelingSampler
from noema.core.protocols.media import FileService, InputViewResolver, MediaBudgetPlanner, MediaNegotiator
from noema.core.protocols.ops import (
    Admission,
    CostMeter,
    HealthRegistry,
    RateLimiter,
    RetryBudget,
    TokenEstimator,
    WorkGraphPlanner,
)
from noema.core.protocols.runtime import ProcessRuntime
from noema.core.protocols.sandbox import SandboxRunner
from noema.core.protocols.storage import Database, IdempotencyStore, JobQueue, Outbox, OutboxRecord
from noema.core.protocols.telemetry import Telemetry
from noema.core.redis_keys import RedisKeys

Doc = Mapping[str, Any]


def principal(
    *,
    tenant_id: str = "t_acme",
    project_id: str = "p1",
    principal_id: str = "prn_dev",
    roles: tuple[Role, ...] = ("developer",),
    scopes: frozenset[Scope] = ALL_SCOPES,
    key_id: str | None = "key_1",
    request_id: str = "req_1",
) -> Principal:
    """A test principal (defaults: a developer of t_acme/p1 with every scope)."""
    return Principal(tenant_id, project_id, principal_id, key_id, frozenset(roles), scopes, request_id)


NullReservation = CountingReservation  # a Reservation that only counts consume() calls


@dataclass
class ClockKit:
    clock: Clock
    advance: Callable[[float], Awaitable[None]] | None  # moves this clock's time (None: a real clock)


@dataclass
class IdGenKit:
    ids: IdGen
    clock: FakeClock  # the clock the generator reads


@dataclass
class ConfigRegistryKit:
    registry: ConfigRegistry
    activate: Callable[[ConfigSnapshot], object]  # make a snapshot current (config watcher stand-in)
    snapshot: Callable[[int], ConfigSnapshot]  # the example config at config_version n


@dataclass
class SecretsKit:
    provider: SecretsProvider
    clock: FakeClock
    put: Callable[[str, bytes, str | None], Awaitable[None]]  # store a version (None = next) and make it current
    backend_reads: Callable[[], int] | None = None  # how often the backend was read (cache checks)
    break_backend: Callable[[str], None] | None = None  # make reads of one ref fail


@dataclass
class BrokerKit:
    broker: CredentialBroker
    clock: FakeClock
    tenant: TenantRef  # a configured tenant whose credentials can be issued
    unknown_tenant: str  # not configured: get() raises TenantAccessMisconfigured
    fail: Callable[[str, str], None] | None = None  # (tenant, cause) -> later get() fails closed with that cause
    issued: Callable[[str], int] | None = None  # STS sessions minted for a tenant (single-flight checks)


@dataclass
class AdapterKit:
    adapters: AdapterRegistry
    clock: FakeClock
    tenant: TenantRef
    use_scenario: Callable[[Doc], None]  # load a scenario DSL document (spec-10 §2.4) into the backend
    wire_requests: Callable[[], list[Mapping[str, Any]]] | None = None  # captured wire bodies (golden checks)
    open_streams: Callable[[], int] | None = None  # live provider streams (cancel = aclose checks)
    virtual_time: bool = True  # backend latency follows `clock` (False: a wall-clock wire mock; timing skipped)


@dataclass
class HealthKit:
    health: HealthRegistry
    retry_budget: RetryBudget
    clock: FakeClock


@dataclass
class DecisionKit:
    engine: DecisionEngine
    compiler: QuestionCompiler
    snapshot: ConfigSnapshot
    clock: FakeClock
    use_scenario: Callable[[Doc], None]
    db_available: Callable[[bool], None] | None = None  # simulate Postgres down for finalize()'s row insert
    outbox_records: Callable[[str], Sequence[OutboxRecord]] | None = None  # records put, by kind


@dataclass
class CompilerKit:
    compiler: QuestionCompiler
    snapshot: ConfigSnapshot


@dataclass
class CacheKit:
    cache: DecisionCache
    clock: FakeClock
    keys: RedisKeys
    break_backend: Callable[[bool], None] | None = None  # True: every call fails (fail-open checks)
    virtual_ttl: bool = True  # TTLs follow `clock` (False: a real store; TTL tests are skipped)


@dataclass
class GenerationKit:
    engine: GenerationEngine
    snapshot: ConfigSnapshot
    clock: FakeClock
    use_scenario: Callable[[Doc], None]
    force_breaker: Callable[[str, str], None] | None = None  # (model id, "open"|"closed")


@dataclass
class EventLogKit:
    log: TurnEventLog
    clock: FakeClock
    other_pod: TurnEventLog | None = None  # a second process sharing the mirror (attach_remote checks)


@dataclass
class SseWriterKit:
    writer: SseWriter
    clock: FakeClock


@dataclass
class IdempotencyKit:
    store: IdempotencyStore
    clock: FakeClock


@dataclass
class AdmissionKit:
    make: Callable[[ConfigRegistry], Admission]  # an admission reading its limits from this registry
    clock: FakeClock


@dataclass
class PlannerKit:
    planner: WorkGraphPlanner
    snapshot: ConfigSnapshot


@dataclass
class CostKit:
    make: Callable[[ConfigRegistry], tuple[CostMeter, Callable[[], list[OutboxRecord]]]]  # meter + its outbox records


@dataclass
class EstimatorKit:
    estimator: TokenEstimator


@dataclass
class JobQueueKit:
    queue: JobQueue
    clock: FakeClock
    job_state: Callable[[str], Awaitable[str]]  # job id -> queued|running|done|dead


@dataclass
class OutboxKit:
    outbox: Outbox
    applied: Callable[[], Awaitable[list[OutboxRecord]]]  # records that reached the sink after flush()


@dataclass
class DatabaseKit:
    db: Database
    clock: FakeClock
    seed: Callable[[str, Mapping[str, Any]], Awaitable[None]]  # (table, row) without RLS (fixtures)
    available: Callable[[bool], None] | None = None  # simulate Postgres down


@dataclass
class MediaKit:
    files: FileService
    views: InputViewResolver
    negotiator: MediaNegotiator
    media_budget: MediaBudgetPlanner
    snapshot: ConfigSnapshot
    clock: FakeClock
    pending_uploads: Callable[[bool], None] | None = None  # new uploads stay processing (derivatives pending)
    reject: Callable[[str], None] | None = None  # move a file to rejected


@dataclass
class SandboxKit:
    runner: SandboxRunner


@dataclass
class CalibratorKit:
    registry: CalibratorRegistry
    add: Callable[[Calibrator, str | None], Awaitable[None]]  # activate an artifact (tenant id or None = platform)
    known_tenant: str


@dataclass
class SamplerKit:
    make: Callable[[ConfigRegistry], tuple[LabelingSampler, Callable[[], list[OutboxRecord]]]]


@dataclass
class AuthKit:
    authn: Authenticator
    authz: Authorizer
    register_key: Callable[[str, Principal], Awaitable[None]]  # make `Authorization: Bearer <token>` valid


@dataclass
class RateLimiterKit:
    limiter: RateLimiter
    clock: FakeClock  # the clock the limiter reads (real kits: also age the backend's buckets)


@dataclass
class RuntimeKit:
    runtime: ProcessRuntime
    clock: FakeClock
    open: Callable[[], Awaitable[None]]  # enter the root task group (composition.start)
    close: Callable[[], Awaitable[None]]  # drain and leave it (composition.stop), in the opening task
    begin_drain: Callable[[], None]  # what SIGTERM triggers


@dataclass
class TelemetryKit:
    telemetry: Telemetry
    spans: Callable[[], list[tuple[str, Mapping[str, Any], str | None]]]  # (name, attributes, error.type)
    metrics: Callable[[], Mapping[tuple[str, tuple[tuple[str, str], ...]], float]]  # (name, labels) -> value
    strict: bool = True
