"""Static Protocol conformance of every fake (checked by `mypy --strict tests/fakes`).

Each assignment below fails type checking if a fake drifts from its frozen Protocol (spec-02). The functions are
never called; `tests/contract_internal` checks the behavior.
"""

from __future__ import annotations

from noema.core.clock import Clock
from noema.core.ids import IdGen
from noema.core.protocols.adapters import Adapter, AdapterRegistry
from noema.core.protocols.auth import Authenticator, Authorizer
from noema.core.protocols.config import ConfigRegistry, ConfigSnapshot
from noema.core.protocols.credentials import CredentialBroker, SecretsProvider
from noema.core.protocols.decision import DecisionCache, DecisionEngine, DecisionRun, QuestionCompiler
from noema.core.protocols.events import SseWriter, TurnEventLog
from noema.core.protocols.generation import GenerationEngine, ProfileRun, PromptBuilder
from noema.core.protocols.labeling import CalibratorRegistry, LabelingSampler, Transform
from noema.core.protocols.media import FileService, InputViewResolver, MediaBudgetPlanner, MediaNegotiator
from noema.core.protocols.ops import (
    Admission,
    CostMeter,
    HealthRegistry,
    RateLimiter,
    Reservation,
    RetryBudget,
    TokenEstimator,
    WorkGraphPlanner,
)
from noema.core.protocols.runtime import ProcessRuntime
from noema.core.protocols.sandbox import SandboxRunner
from noema.core.protocols.storage import (
    AttemptRepo,
    AuditRepo,
    CalibratorRepo,
    Database,
    DecisionRepo,
    DerivativeRepo,
    FileRepo,
    IdempotencyRepo,
    IdempotencyStore,
    JobQueue,
    JobRepo,
    LabelRepo,
    Outbox,
    ProjectRepo,
    SessionRepo,
    SystemCalls,
    TurnRepo,
    UnitOfWork,
    UsageRepo,
    Worker,
)
from noema.core.protocols.telemetry import Instruments, Span, Telemetry

from .clock import FakeClock, FakeIdGen
from .db import FakeUnitOfWork
from .db_system import FakeSystemCalls
from .decision_engine import FakeDecisionRun
from .generation import FakeProfileRun, StaticPromptBuilder
from .labeling import IdentityTransform, PlattTransform, TemperatureTransform
from .media_views import FakeSandboxRunner
from .ops_admission import FakeRateLimiter, FakeReservation
from .telemetry import FakeSpan
from .world import FakeWorld


def world_conforms(w: FakeWorld) -> None:
    """Every `Services`-level fake against its Protocol."""
    clock: Clock = FakeClock()
    ids: IdGen = FakeIdGen(clock)
    config: ConfigRegistry = w.config
    snapshot: ConfigSnapshot = w.config.current()
    secrets: SecretsProvider = w.secrets
    broker: CredentialBroker = w.broker
    adapters: AdapterRegistry = w.adapters
    adapter: Adapter = w.adapters.for_model("sol-6.1")
    health: HealthRegistry = w.health
    retry_budget: RetryBudget = w.retry_budget
    planner: WorkGraphPlanner = w.planner
    admission: Admission = w.admission
    estimator: TokenEstimator = w.estimator
    cost: CostMeter = w.cost
    outbox: Outbox = w.outbox
    db: Database = w.db
    jobs: JobQueue = w.jobs
    idempotency: IdempotencyStore = w.idempotency
    events: TurnEventLog = w.events
    sse: SseWriter = w.sse
    files: FileService = w.files
    views: InputViewResolver = w.views
    negotiator: MediaNegotiator = w.negotiator
    media_budget: MediaBudgetPlanner = w.media_budget
    calibrators: CalibratorRegistry = w.calibrators
    sampler: LabelingSampler = w.sampler
    compiler: QuestionCompiler = w.compiler
    cache: DecisionCache = w.decision_cache
    decisions: DecisionEngine = w.decisions
    generation: GenerationEngine = w.generation
    authn: Authenticator = w.authn
    authz: Authorizer = w.authz
    telemetry: Telemetry = w.telemetry
    instruments: Instruments = w.telemetry.instruments
    runtime: ProcessRuntime = w.runtime
    limiter: RateLimiter = FakeRateLimiter(clock)
    del runtime, limiter
    del clock, ids, config, snapshot, secrets, broker, adapters, adapter, health, retry_budget, planner, admission
    del estimator, cost, outbox, db, jobs, idempotency, events, sse, files, views, negotiator, media_budget
    del calibrators, sampler, compiler, cache, decisions, generation, authn, authz, telemetry, instruments


def inner_conforms(
    uow: FakeUnitOfWork,
    system: FakeSystemCalls,
    drun: FakeDecisionRun,
    prun: FakeProfileRun,
    reservation: FakeReservation,
    span: FakeSpan,
    worker_queue: FakeWorld,
) -> None:
    """Objects the factories hand out (units of work, repos, runs, reservations, spans, workers)."""
    u: UnitOfWork = uow
    repos: tuple[
        SessionRepo,
        TurnRepo,
        AttemptRepo,
        DecisionRepo,
        FileRepo,
        DerivativeRepo,
        UsageRepo,
        LabelRepo,
        CalibratorRepo,
        IdempotencyRepo,
        AuditRepo,
        ProjectRepo,
        JobRepo,
    ] = (
        uow.sessions,
        uow.turns,
        uow.attempts,
        uow.decisions,
        uow.files,
        uow.derivatives,
        uow.usage,
        uow.labels,
        uow.calibrators,
        uow.idempotency,
        uow.audit,
        uow.projects,
        uow.jobs,
    )
    s: SystemCalls = system
    d: DecisionRun = drun
    p: ProfileRun = prun
    r: Reservation = reservation
    sp: Span = span
    builder: PromptBuilder = StaticPromptBuilder()
    transforms: tuple[Transform, Transform, Transform] = (
        IdentityTransform(),
        TemperatureTransform(1.0),
        PlattTransform(1.0, 0.0),
    )
    sandbox: SandboxRunner = FakeSandboxRunner()

    async def handler(_job: object) -> None: ...

    worker: Worker = worker_queue.jobs.worker(["k"], handler, concurrency=1)
    del u, repos, s, d, p, r, sp, builder, transforms, sandbox, worker
