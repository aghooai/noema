# 01 — Module map, layers and the composition root

Wave A, lead architect. This file refines `.orchestration/context/02-interfaces-ownership.md`; the ownership table there stays authoritative for paths, with three Wave A2 overrides:
- the "repo" entries of `decision/`, `turns/` and `identity/` mean row mapping only (`repo.py`, no SQL); every SQL statement is in the frozen `db/repos/` (§1);
- `src/noema/entrypoints/` belongs to the integrator (§1, §4);
- its "Interfaces to freeze" list is superseded by spec-02 (for example `AttemptHandle` is now `ProfileRun`, `GenerationEngine.start` is `run`, `DecisionEngine.start` is `run`).

B1 encodes the layers below in `.importlinter`.

## 1. Packages

```text
src/noema/
  core/          FROZEN  ids, clock/Deadline, errors (ErrorCode, ERROR_TABLE, ErrorKind), values, principal, text helpers,
                         secret, redis_keys, objects (bucket, SSE-KMS alias), settings (ProcessSettings), runtime
                         (TaskGroupRuntime), protocols/{config,credentials,adapters,decision,media,sandbox,storage,events,ops,
                         labeling,generation,auth,telemetry,runtime,services}.py                 (spec-02)
  contracts/     FROZEN  _vendor (byte copy of docs/noema/contracts), build/openapi.bundled.yaml, _generated/models.py,
                         gate.py (RequestGate), rules.py, validate.py (ContractValidator, tests only)  (IMPL-041)
  config/        FROZEN  models.py (*Cfg), loader.py, rules.py (P1 rules), registry.py, prices.py  (spec-05)
  db/            FROZEN  alembic/, engine.py (roles), uow.py, system.py (SystemCalls), repos/ (spec-03)
                         repos/ holds the only SQL of the app: one class per spec-02 §7.1 repo Protocol. The `repo.py`
                         files of decision/turns/identity map domain values to and from spec-03 rows; they contain no SQL
  api/           FROZEN  app.py, deps.py, middleware/ (request id, trace, body cap, auth, gate, idempotency, errors), sse_frame.py
  api/routes/            stubs by foundation; bodies by the owners in 02-interfaces-ownership.md (no metrics.py: `/metrics`
                         is served by `composition.start` on NOEMA_METRICS_PORT, the telemetry Prometheus reader)
  identity/      apikeys, authn, authz (spec-09 §3), sts.py (ADR-001), secrets.py, sessions service, cli/noema_admin
  adapters/      converse.py, mantle.py, capabilities.py, classify.py (spec-02 §4.3), resilience/{breakers,retry_budget,timeouts}
  decision/      spec-06 §1
  generation/    engine, profiles/{single,fallback,race}, attempt FSM, holdback buffer, cancel estimates
  streaming/     event_log (in-process + Redis mirror), sse_writer, backpressure
  turns/         orchestrator, spec, bindings, conditioning, late policy, verification, session ordering, preflight,
                 context/, prompt/ (PREAMBLE_V1, spotlighting)                                      (spec-04)
  media/         intake, quarantine, pipeline, sandbox/{remote,nsjail,bwrap,inproc,manifest}, extractors/, worker,
                 negotiation.py, budget.py, views.py                                                 (spec-07, ADR-002)
  ops/           admission/ (GCRA Lua, work graph, WFQ, bulkheads), cost, prices, usage, jobs, outbox, idempotency,
                 retention, reapers, notify_bridge (pg_notify -> Redis pub/sub, spec-11 §4)            (spec-11)
  labeling/      sampler, labelers (D4), jobs, calibrators/{temperature,platt}, metrics, acceptance, registry   (ADR-003)
  telemetry/     FROZEN  otel setup, GenAI attribute mapper, instruments, redact
  composition.py integrator: wires the real implementations (seeded with fakes by foundation)
  entrypoints/   integrator: jobs.py, media_worker.py: build Services, `start()`, then run `services.jobs.worker(kinds, dispatch)`;
                 each owner's jobs module exports `make_handlers(services: ServiceSet) -> JobHandlers` (exact kind -> handler),
                 merged with `merge_handlers` (a kind claimed twice is an error); `--kinds` globs expand with `expand_kinds`
                 (`core/protocols/services.py`, spec-11 §4)
tools/mockbedrock/  qa-tools wire mock (spec-10 §2)
sdk/typescript/, sdk/python/   sdk (IMPL-035)
web/            ui (spec-09 §8)
```

## 2. Import layers (import-linter contracts)

1. `core` imports nothing from `noema` except itself.
2. `contracts`, `db` and `telemetry` import only `core`. `config` imports `core` and `contracts` (its `rules.py` wraps the vendored `check_config.semantic_errors`).
3. Domain modules import `core` Protocols and value types, plus `contracts`, `config`, `db`, `telemetry`. Each domain module is `identity`, `adapters`, `decision`, `generation`, `streaming`, `turns`, `media`, `ops`, `labeling`.
4. Domain modules never import each other. They interact only through `core.protocols` and the value types in `core` (for example `CompiledQuestionSet`, `AnswerSummary`, `AttemptSpec`), with implementations injected by `composition.py`. Two packages have two owners and get an extra independence contract:
   - `noema.media.{negotiation,budget,views}` ⟂ `noema.media.{intake,quarantine,pipeline,sandbox,extractors,worker}`;
   - `noema.ops.admission` ⟂ `noema.ops.{cost,prices,usage,jobs,outbox,idempotency,retention,reapers,notify_bridge}`.
5. `api.app` and `api.deps` import `composition` (the lifespan calls `build()`); `api.middleware` and `api.sse_frame` import only `core`, `contracts`, `config`, `telemetry`. `api.routes.<x>` import `api.deps`, `core`, `contracts`, `config`, and only the domain package of the route's owner (the ownership table in `02-interfaces-ownership.md`): `turns` → `noema.turns`; `decisions`, `openai_compat`, `profiles` → `noema.decision`; `sessions` → `noema.identity`; `files` → `noema.media`; `usage`, `health` → `noema.ops`; `admin_readonly` → `noema.labeling`. Services always come from `api.deps`. Each route has two forbidden contracts: no direct import of `db`, `telemetry`, `composition`, `entrypoints` or `api.middleware`, and no other domain package; indirect chains through the route's own domain are allowed.
6. `composition` may import everything; nothing imports `composition` except `api.app`, `api.deps` and `noema.entrypoints.*` (so no domain module imports it, which would be a cycle). `media.sandbox.sandboxd` and `identity.cli` don't use `composition`: the jailer has no DB, Redis or network, and the CLI opens its own `noema_cli` connection through `noema.db`.
7. Heavy media dependencies (the `media` dependency group) are forbidden everywhere except `noema.media.extractors`: `PIL`, `pillow_heif`, `pikepdf`, `docling`, `docling_core`, `docling_parse`, `torch`, `onnxruntime`, `pypdfium2`, `pytesseract`, `magika`, `magic`, `markdown_it` (ADR-002 §1; nh3 is not a Phase-1 dependency, spec-09 §6.5). The API, worker and jobs images are built without the group, so a violation also fails at import time.
8. Forbidden everywhere: `openai` (the PyPI client is used only by `tests/integration/decision/test_facade_openai_sdk.py`), `anthropic`, `requests` (use httpx), `boto3` (use `aiobotocore`).

`.importlinter` sets `include_external_packages = True` (required for the forbidden-module contracts of rules 7 and 8) and has one contract per rule: `layers` for 1–3 and 6, `independence` for 4 (the domain packages, plus the two intra-package pairs), `forbidden` for 5, 7 and 8. Tests are not linted by import-linter.

## 3. Composition root (`composition.py`, integrator)

```python
@dataclass(frozen=True)
class Services:                     # one per process; built in the app lifespan
    clock: Clock; ids: IdGen; config: ConfigRegistry; secrets: SecretsProvider; broker: CredentialBroker
    adapters: AdapterRegistry; health: HealthRegistry; retry_budget: RetryBudget; planner: WorkGraphPlanner
    admission: Admission; estimator: TokenEstimator; cost: CostMeter; outbox: Outbox; db: Database; jobs: JobQueue
    idempotency: IdempotencyStore; events: TurnEventLog; sse: SseWriter; files: FileService; views: InputViewResolver
    negotiator: MediaNegotiator; media_budget: MediaBudgetPlanner; calibrators: CalibratorRegistry; sampler: LabelingSampler
    compiler: QuestionCompiler; decision_cache: DecisionCache; decisions: DecisionEngine; generation: GenerationEngine
    authn: Authenticator; authz: Authorizer; telemetry: Telemetry
    redis: "redis.asyncio.Redis"; keys: RedisKeys        # bytes client (decode_responses=False); every key via `keys`
    s3: "S3Client"                                       # aiobotocore, platform credentials; bucket settings.s3_bucket (spec-03 §2)
    runtime: TaskGroupRuntime                            # ProcessRuntime (core/protocols/runtime.py): spawn, on_drain, draining, local
    settings: ProcessSettings                            # core/settings.py: the spec-05 §5 variables, parsed by process_settings()
def build(env: Literal["test", "dev", "staging", "prod"], *, fakes: frozenset[str] = frozenset(),
          environ: Mapping[str, str] | None = None) -> Services: ...   # sync; async clients are holders filled by start()
async def start(services: Services, process: Literal["api", "jobs", "media-worker"]) -> None: ...   # background tasks below
async def stop(services: Services) -> None: ...      # reverse order; spec-11 §7 shutdown
```

`fakes` names `Services` fields (for example `frozenset({"adapters", "broker"})`); each named field gets its `tests/fakes` implementation.

Domain code types its dependencies with `ServiceSet` (`core/protocols/services.py`: every `Services` field, read-only), never with `composition.Services`; `noema.api.deps.ApiServices` is the same type. `services.runtime` is the process's root task group: `spawn(name, fn)` for work that outlives a request (failures are logged, never raised), `on_drain(name, cb)` for shutdown hooks (`cb(grace_s)`), `draining` (`/readyz` 503) and `local(key, factory)` for process state such as `"turns.registry"` (no module globals). `services.settings` holds `pod_id` (lease and job owners), `s3_bucket`, `redis_url`, `db_dsn_ref`, `drain_delay_s`, `metrics_port` and the endpoint overrides. Every tenant object put passes `core.objects.sse_args(env, tenant_id)` (SSE-KMS `alias/noema-<env>-<tenant_id>` in staging and prod, AES256 in dev and test).

`build()` refuses disallowed environment combinations (the variables are listed in spec-05 §5): AmbientBroker outside dev/test, endpoint overrides (`NOEMA_AWS_ENDPOINT_OVERRIDES`) outside dev/test, the InprocRlimit sandbox outside test, `NOEMA_CONTRACT_VALIDATE` in prod (ADR-001 §7-8, ADR-002 §7, IMPL-041).

Background tasks started by `start()` (each in the process's root task group; cancelled by `stop()`):

| Task | Processes | Spec |
|---|---|---|
| config watcher (`config_changed` + 5 s poll of the active `platform.config_version`) | all | spec-05 §3 |
| calibrator registry refresh (`calibrators_changed` + 5 s poll) | api, jobs | spec-02 §9 |
| key-revocation listener (`keys_revoked` → LRU evict) | api | IMPL-008 |
| breaker / retry-budget sync (pub/sub, 1 s) | api, jobs | IMPL-078 |
| outbox batch writer and drainer | api, jobs | spec-11 §3 |
| running-turn registry refresh, cancel listener, abandon watchdog | api | spec-04 §1, §11 |
| job workers, periodic-job scheduler, reaper, notify bridge | jobs (job workers also in media-worker) | spec-11 §4, §6 |
| `/metrics` server (the telemetry Prometheus reader, when NOEMA_METRICS_PORT is set); SIGTERM chain (draining first) | all; SIGTERM chain api | spec-11 §7 |

## 4. Processes

| Process | Entry point | Contents |
|---|---|---|
| `api` | `uvicorn noema.api.app:create_app --factory --loop uvloop --http httptools` (`--factory` is required; add `--timeout-graceful-shutdown` above the 30 s drain grace, spec-11 §7) | HTTP/SSE, orchestrator, engines, adapters; `/metrics` on NOEMA_METRICS_PORT |
| `jobs` | `python -m noema.entrypoints.jobs --kinds labeling.*,session.delete,retention.sweep`; handlers from `noema.labeling.jobs`, `noema.identity.jobs`, `noema.ops.retention` | job runner, with tenant STS; periodic-job scheduler, reaper, notify bridge (spec-11 §4, §6) |
| `media-worker` | `python -m noema.entrypoints.media_worker --kinds media.process,file.purge`; handlers from `noema.media.worker` | spooler and the `media.process` and `file.purge` job workers; no parser libraries (ADR-002) |
| `media-sandbox` | `python -m noema.media.sandbox.sandboxd` | nsjail jailer; `network_mode: none` |
| `noema-admin` | `python -m noema.identity.cli` | operator CLI (IMPL-044) |
