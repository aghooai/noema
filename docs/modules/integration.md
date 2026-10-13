# Integration: composition root, worker entrypoints, compose stack, bootstrap, e2e fixtures (owner: integrator)

Wave C2b/D. Paths: `src/noema/composition.py`, `src/noema/entrypoints/**`, `deploy/**` (with infra),
`tools/bootstrap_dev.py`, `make/e2e.mk`, `tests/e2e/conftest.py`, `tests/e2e/smoke/**`, `tests/{unit,integration}/
composition/**`. Specs: spec-01 §3-§4, spec-05 §3 and §5, spec-11 §4 and §7, ADR-001 §7-§8, ADR-002 §6-§7, IMPL-041;
the wiring follows `.orchestration/reports/c2b-integrator-brief.md` and every module's "Wiring" section.

## 1. `composition.build(env, *, fakes=frozenset(), environ=None, wrap_adapters=None, process="api") -> Services`

`build()` is sync and opens no connection. It validates the spec-05 §5 environment (`process_settings`), then wires
the REAL implementation of every field in dependency order; a field named in `fakes` gets its `tests/fakes` one
(dev/test only; `tests.fakes.world.build_world` is imported only when `fakes` is non-empty, so the images need no
`tests` package). `wrap_adapters(registry) -> registry` is applied right after the adapter registry is built and
before the decision, generation and labeling code capture it (the live smoke's `CountingAdapterRegistry`).
`process` names the telemetry resource (`service.name` = `noema-<process>`; the worker entrypoints pass theirs, the
app lifespan builds the api's), so traces and metrics tell api, jobs and media-worker apart (C2b/D).

| Field | Implementation (module) | Built from |
|---|---|---|
| clock, ids, keys, telemetry, redis, runtime | `SystemClock`, `UlidIdGen`, `RedisKeys(ns)`, `OtelTelemetry`, `redis.asyncio.Redis` (bytes, lazy, TCP keepalive 30 s idle + 3 x 10 s probes: Wave E, so a pub/sub listener's peer that died without FIN/RST is noticed; one `BlockingConnectionPool` of `NOEMA_REDIS_MAX_CONNECTIONS` connections, default 256, 8-10,000, pub/sub included, a caller waits at most 0.5 s for a free one: Wave E3, IMPL-088), `TaskGroupRuntime` | settings (`ns` = `noema:`; in test a per-process `ns:<agent>:<pid>:<hex>:`) |
| config | `ActiveConfigRegistry` (composition) | the active `platform.config_version` (holder; see §2) |
| secrets | `SecretsManagerProvider` (identity) | `us-east-1`, the `secretsmanager` override |
| s3 | `_LazyS3` over identity's `LazyClient("s3")` | pod credentials, the `s3` override (path-style), 64 pooled connections |
| db | `PgDatabase(_DbPool, make_unit_of_work)` (db) | the noema_app DSN of the secret `settings.db_dsn_ref` |
| outbox | `WriteBehindOutbox(db, redis, keys, clock, instruments)` (ops) | + `OutboxDrainer(..., consumer=pod_id)` |
| jobs | `PgJobQueue(db, clock, ids, pod_id=, listener=_DeferredListener, redact=telemetry.redact)` | |
| estimator, cost, idempotency | `HeuristicTokenEstimator(table=)` (`TOKEN_ESTIMATOR_V1`, or the table of `NOEMA_TOKEN_ESTIMATOR_TABLE`: an absolute path to `estimator_fit`'s JSON; an unreadable or invalid file stops the process, IMPL-029c), `LedgerCostMeter(clock, config, outbox, instruments)`, `PgIdempotencyStore(db, redis, keys, BodyStore(s3, bucket, env), clock, ids, config, instruments, pod_id=)` | |
| retry_budget | `SlidingRetryBudget(clock, keys=, pod_id=)`: ONE instance, also both adapters' (`note_request`) | |
| health | `BreakerRegistry(clock, policy=, telemetry=, keys=)` | shared by generation and decision (provider-health outcomes only); `NOEMA_BREAKER_TTFT_RULE=off` (dev/test only) passes `BreakerPolicy(ttft_factor=inf)`: the p95-TTFT rule disarmed, the failure-ratio rule kept |
| planner, admission | `DefaultWorkGraphPlanner()`, `GcraAdmission(clock, config, keys, redis, env=, instruments=, expected_pods=)` | |
| broker | `StsBroker(config, secrets, sts=AioStsClient(sts override), clock, runtime, deployment=env, pod_id, telemetry, security)`; `AmbientBroker` with `NOEMA_CREDENTIALS_BROKER=ambient` (dev/test) | |
| adapters | `AdapterRegistryImpl(config, {"bedrock-converse": ConverseAdapter, "bedrock-mantle-chat": MantleAdapter})`, then `wrap_adapters` | both adapters: config, broker, clock, the one retry budget, telemetry, identity's `SecurityEvents`, the endpoint overrides |
| events, sse | `InProcessTurnEventLog(clock, mirror=RedisEventMirror(redis, keys, clock))`, `BackpressureSseWriter(clock)` | |
| files | `MediaFileService.from_services(<fields built so far>)` | db, jobs, s3, config, redis, keys, outbox, telemetry |
| views, negotiator, media_budget | `InputViewResolverImpl(files)`, `MediaNegotiatorImpl(files)`, `MediaBudgetPlannerImpl()` | |
| calibrators, sampler | `DbCalibratorRegistry(db)` (concrete: its `listen` is wired), `Sampler(clock, config, outbox, expected_pods=)` | |
| compiler, decision_cache, decisions | `QuestionCompilerImpl(config)`, `DecisionCacheImpl(redis, keys, clock, instruments)`, `DecisionEngineImpl(EngineDeps(...))` | the Services fields of the same names |
| generation | `StreamingGenerationEngine(clock, ids, adapters, broker, health, retry_budget, estimator, cost, telemetry)` | |
| authn, authz | `ApiKeyAuthenticator(db, secrets, limiter=GcraRateLimiter(clock, keys, redis, expected_pods=), clock, runtime, env)`, `MatrixAuthorizer()` | the per-IP `authfail:<ip>` bucket |

ADR-001 §4 tenant credentials (brief A3): the adapters sign every call with the credentials of that call's own
`broker.get` + `assert_creds_for` (the broker's 600/300 s windows; E sprint A, adapters.md), so composition injects
nothing. `NOEMA_DEV_API_KEY` registers on the fake authenticator only; the real one logs a
warning and ignores it. `build()` keeps every refusal of the seed (fakes, endpoint overrides, `NOEMA_DEV_API_KEY`
outside dev/test; `NOEMA_CONTRACT_VALIDATE` in prod; the bwrap/inproc sandbox backends) and adds
`NOEMA_CREDENTIALS_BROKER` (`sts` | `ambient`, `check_component("broker.<kind>", env)`) and `NOEMA_BREAKER_TTFT_RULE`
(`on` | `off`, `off` only in dev/test: `check_component("breaker.ttft_off", env)`).

## 2. Holders (async construction behind the sync build)

- `_LazyS3`: every attribute is the entered client's async method (`await s3.put_object(...)`); `start()` opens it,
  `stop()` closes it.
- `_DbPool`: the DSN secret is read and the pool created by `start()` or the first unit of work; a failed read or
  creation is the retryable `internal_error` of an unreachable Postgres, retried at most once a second with the
  secret read again. So a process may start before its stack is seeded and heals once the secret and database
  exist. A created pool keeps its DSN (§9).
- `_DeferredListener`: the job workers' and the notify bridge's `PgListener`, created once the DSN is readable.
- `ActiveConfigRegistry`: `current()`/`get()` raise a retryable `internal_error` until the first load (`/readyz` 503);
  `refresh(db)` reads `SystemCalls.active_config()` (and the `db:<n>` catalog), loads it with `config.loader` (env =
  the process env) and activates it when version or digest changed; a rejected version (loader error, digest
  mismatch, or any other exception while loading that body) keeps the current snapshot and is logged at ERROR once. Callbacks registered before the first load are
  called with it. The watcher (`config.watch`, every process) refreshes on `config_changed` (pub/sub, reconnect
  1..30 s) and every 5 s.

## 3. `start(services, process)` and `stop(services)`

`start` opens the runtime, installs the redacted JSON log handler (real telemetry outside test; `httpx`/`httpcore`
at WARNING), runs each wired component's start steps for `process` in build order, spawns their background tasks,
registers their drain hooks, then the calibrator poll (api, jobs), the event-loop lag sampler (every process),
`TurnService.of(services).start_background()` (api), the Prometheus listener (`NOEMA_METRICS_PORT`) and the
SIGTERM chain (api). Every background loop runs under `composition.supervised(name, run, clock)`: a loop that raises
is logged at ERROR and restarted after 1, 2, 4 ... 30 s (reset after a run of 30 s), so one bad iteration (e.g. in
the config watcher) cannot stop it for the life of the pod while `/readyz` stays 200; the entrypoints' job worker
too. The SIGTERM handler starts the drain inside a task (`loop.call_soon_threadsafe` -> `loop.create_task`): anyio
finds its backend through sniffio's current task, which a plain loop callback lacks, so with sniffio importable no
drain hook would start (C2b/D fix, `tests/unit/composition/test_sigterm.py`). Outside test processes it
then collects once and freezes the heap (`gc.collect(); gc.freeze()`, `_freeze_startup_heap`): the startup objects
(modules, botocore models, schema validators, the config) move to the permanent generation, so later full
collections scan only the request-time heap (C2b perf smoke: a full GC of the api at 50 streams took 192 ms before,
47 ms after; `tests/unit/composition/test_heap.py`), then sets the GC thresholds to `(700, 10, 100)` unless they were
already changed (E sprint B's `(700, 10, 40)`: a full collection every ~5 s instead of ~1.5 s at 100 streams per
process, TTFT overhead p95 124 -> 72 ms, p99 175 -> 118 ms, reports/perf/e-sprint-b.md section 6; Wave E3's
`(700, 10, 100)`: every ~21 s instead of 8.6 s at 200 streams per pod, p95 25.8 -> 23.7 ms, reports/qa/e3/perf-gc-objects.md
section 2 item 10). A failing start is undone with `stop()` and re-raised.

| Start step / task (name) | api | jobs | media-worker | Spec |
|---|---|---|---|---|
| `identity.secrets`, `s3`, `db.pool` (warm), `config.load` | x | x | x | |
| task `telemetry.loop_lag` (`aghoo_event_loop_lag_seconds` = how late a 0.5 s sleep wakes; the 08 §9 HPA signal) | x | x | x | spec-10 §5 |
| task `config.watch` (`config_changed` + 5 s poll) | x | x | x | spec-05 §3 |
| task `telemetry.pool_gauges` (real database only; every 1 s `aghoo_db_pool_size` / `aghoo_db_pool_in_use` `{pool="app"}`, `aghoo_redis_pool_size` / `aghoo_redis_pool_in_use`) | x | x | x | IMPL-088, spec-10 §5 C2 |
| `identity.sts`, `identity.broker`, `adapters.{converse,mantle}.prewarm`, `adapters.probe_check` | x | x | | ADR-001 |
| `identity.authn` (pepper ring); task `identity.keys_revoked` | x | | | IMPL-008 |
| tasks `adapters.breaker_sync`, `adapters.retry_budget_sync` (1 s) | x | x | | IMPL-078 |
| tasks `ops.outbox.writer` (all), `ops.outbox.drainer` | x | x | writer | spec-11 §3 |
| tasks `labeling.calibrators_listen`, `calibrator-poll` (5 s) | x | x | | spec-02 §9 |
| `streaming.event_log` task + `streaming.mirror` drain hook; turn service (`turns.cancel_listener`, `turns.owner_refresh`, `turns.watchdog`, drain `turns.drain`) | x | | | spec-04 §1, §11 |
| tasks `ops.jobs.scheduler`, `ops.reapers.jobs`, `ops.notify_bridge` | | x | | spec-11 §4, §6 |
| the job worker (`ops.jobs.worker` task + drain hook, by the entrypoint) | | x | x | spec-11 §4 |

`stop` (idempotent, shielded): restore SIGTERM; drain (api `DRAIN_GRACE_S + 10` = 40 s, so every turn finalized;
jobs and media-worker 10 s for the running handlers, spec-11 §7); `Outbox.flush(10)`; close the runtime (cancels the
tasks); the close steps in reverse build order (adapters, broker, STS client, DB pool, secrets client); Redis and S3;
the telemetry providers.

## 4. Worker entrypoints

`python -m noema.entrypoints.jobs [--kinds labeling.*,session.delete,retention.sweep] [--concurrency 8]` and
`python -m noema.entrypoints.media_worker [--kinds media.process,file.purge] [--concurrency 8]` (`_worker.serve`):
build from `NOEMA_ENV` (`process=` names the telemetry resource), `start(services, process)`, `merge_handlers` over the owners' `make_handlers(services)`
(jobs: `noema.identity.jobs`, `noema.labeling`, `noema.ops.retention`; media-worker: `noema.media.worker`, whose
runner comes from `NOEMA_SANDBOX_BACKEND` and is gated by `assert_backend_allowed(..., process="media-worker")`),
`expand_kinds` (a pattern without a handler is a startup error, exit 2), one `services.jobs.worker(...)` (spawned
under `supervised`) with `dispatch(handlers)` (an unknown kind is a `PermanentJobError`), until SIGTERM/SIGINT, then `stop()` (exit 0).

## 5. Compose `app` profile and images (deploy/, with infra)

`api-1`, `api-2` (hostnames = pod ids; uvicorn `--timeout-graceful-shutdown 35`, stop grace 45 s, healthcheck
`/readyz`; each one 2-vCPU pod (`cpus: 2`) of `WEB_CONCURRENCY` = 2 uvicorn worker processes sharing port 8000 and
publishing metrics 9464 and 9465, `NOEMA_MAX_TURNS_PER_PROCESS` 120; §5.1), `jobs` and `media-worker`
(healthcheck `/metrics` on 9464, stop grace 35 s; the media-worker runs `--kinds media.process,file.purge`), `media-sandbox`, `mockbedrock` (O(1) healthcheck `GET /_mock/requests?limit=0`;
starts with `deploy/compose/mockbedrock/default.yaml`, `NOEMA_MOCK_SCENARIO` picks another file there), `toxiproxy`
(profiles app and fault) and the base `postgres`, `valkey`, `moto`. App services depend on all of them being
healthy. `NOEMA_STACK_ENV` defaults to `dev` (C2b/D: `test` gives each process its own Redis namespace and no
`/metrics`, so two replicas would share nothing and the worker healthchecks fail); start the profile with
`make e2e-up`, not `make up PROFILES=app`. `NOEMA_STACK_BREAKER_TTFT_RULE` (default `on`) is the app containers'
`NOEMA_BREAKER_TTFT_RULE`. Fault runs: `NOEMA_STACK_REDIS_URL=redis://toxiproxy:16379/0`, `NOEMA_STACK_MOTO_URL=http://toxiproxy:15000`
and the bootstrap `--db-host toxiproxy:15432` (`make e2e-up E2E_DB_HOST=toxiproxy:15432` plus the two variables).
`make e2e-images` (= `image-api image-worker image-media`) rebuilds the images; the api/worker images ship no
`tests` package and build the real composition (tests/integration/infra/test_images.py).

The default scenario answers every router-default and answer-verifier question of the vendored config, the
`department`/`urgency` questions of the contract examples and every other predicate (false 0.95), and streams
"Paris is the capital of France. It is also its largest city." for every ConverseStream / chat-stream call.

### 5.1 Two api processes per pod (E sprint B, reports/perf/e-sprint-b.md)

One api process is one event-loop thread: at the L1 workload it is CPU-bound at about 100-150 streams, so a 2-vCPU
pod (08 §9) runs two: `WEB_CONCURRENCY=2` (uvicorn's default for `--workers`; the image CMD is unchanged, so set it
on the api containers only, never on jobs or media-worker) gives a supervisor plus two worker processes that accept
on the same socket. The per-pod density is 08 §9's (the measured knee of the Wave F image: at most 125 streams per
pod, planned at 100; reports/perf/sizing.md §5, IMPL-089). What the composition does with `WEB_CONCURRENCY` = N > 1
(`composition.api_workers`, `process_settings`, `serve_metrics`):
- `pod_id` = `<NOEMA_POD_ID or host name>-<pid>`: the workers of a pod never share an idempotency owner, a
  `turn_owner`, the retry-budget hash field or the outbox consumer name (two processes with one consumer name
  would ack each other's pending entries); a restarted worker is a new pod, as a restarted pod is;
- `expected_pods` = NOEMA_EXPECTED_PODS x N: the Redis-down local shares divide by every process;
- metrics: each worker serves the first free port of `NOEMA_METRICS_PORT` .. `+ N - 1` (compose publishes 9464 and
  9465; scrape every worker port; the e2e helpers read 9464, one worker's view, which the shared breaker events
  keep in step with the other's);
- per-process limits stay per process (DB pool 20, the Redis client, `NOEMA_MAX_TURNS_PER_PROCESS`), so a pod holds
  up to 2 x 20 Postgres connections.
- images built before this change bind `NOEMA_METRICS_PORT` in both workers: the second fails to start (Errno 98) and
  uvicorn's supervisor stops the container, which restarts in a loop; rebuild the images with the compose file
  (`make e2e-up` does) or start such images with `NOEMA_STACK_API_WORKERS=1`.

Overload (`noema.api.overload`): above `NOEMA_MAX_TURNS_PER_PROCESS` (default 120; 0 = off; an overload guard,
240 per pod = 1.9x the 125-stream knee, not the SLO: IMPL-087) running or starting turns and decisions in flight,
createTurn, createDecision and the facade answer 429 `queue_timeout` (`retry-after: 1`) before any session,
admission or model work, the code spec-11 §1.1 gives to per-pod capacity exhaustion; `aghoo_turns_shed_total`
counts them (decisions included). The cap is checked twice (Wave E3, the IMPL-087 amendment):
- **Edge shed** (`noema.api.shed`, before the middleware stack, ~10-17 µs of CPU per refusal): only for an unkeyed
  createTurn, createDecision or facade `POST`, only while the process holds at least `cap` slots (or, for
  createTurn, some tenant is saturated), and only for a key id the authenticator's in-process key cache holds; the
  tenant is read from the cache, not verified. While the edge sheds, a wrong secret for a cached key id of a
  refused tenant therefore gets 429, not 401, and the request gate's 400/413/415 are not reached; a key id the
  cache does not hold always gets the full ingress (its 401 keeps precedence). Edge refusals get no access line:
  one `edge shed` line per second at most.
- **Route** (`noema.api.routes.capacity`, after authentication and the request gate): the authoritative check.

Fairness (`TurnSlots`, max-min since Wave E3): each tenant's share is the max-min fair share of the cap among the
tenants holding slots plus the caller (water filling, `fair_share`; Wave E used cap ÷ the tenants holding slots). A
tenant below its share is admitted up to `cap // 10` above the cap, a tenant at or above it only below the cap, so
one tenant can still use the whole cap alone but never keeps another tenant out (at most cap + cap // 10 per
process). A tenant whose turn timed out in the admission queue is saturated for 2 s (`SATURATED_S`): meanwhile it
holds at most `cap // 10` waiting createTurns per process, which do not count against the other tenants, and its
further createTurns are refused at once (C5 victim p95 ratio 1.021 / 1.022, reports/qa/wave-e-summary.md). A keyed
request is never shed at the edge, and in the route not before step 7: an idempotent duplicate attaches or replays
without a slot, and only a new keyed request is shed then (its record is not stored, so the retry runs it). The
streams-per-pod scale signal of 08 §9 is `aghoo_turn_slots_held` (`TurnSlots.held()` of one process, set every 1 s
by the api lifespan's task `api.turn_slots_held`, `noema.api.app`; sum it per pod).

## 6. Bootstrap (`tools/bootstrap_dev.py`, `make e2e-bootstrap`)

`python -m tools.bootstrap_dev --compose-project noema-e2e [--env dev] [--db-host postgres:5432] [--state PATH]`
finds the published ports with `docker compose port` and seeds, idempotently (a re-run reuses the state file and adds
what is missing; dev/test only, moto and the mock only):
1. Postgres roles (dev passwords), database `noema`, `alembic upgrade head` (0003).
2. moto: bucket `noema-<env>`; secrets `noema/api-key-pepper` (`{"version": 1, "pepper": ...}`, kept once
   created), `noema/input-digest-pepper`, `noema/db-dsn` (in-stack host) and `noema/db-dsn-host` (published port).
3. noema-admin in-process (`amain`): operators `noema-bootstrap-a` (bootstrap) and `-b`; the dev price catalog
   (`prices import` by A, once per stack: every model of the example config in us-east-1 at FAKE fixed prices,
   input 1.5 / output 6 / cache read 0.15 / cache write 1.875 (5 m) and 3 (1 h) USD per million tokens, never AWS
   prices; `tools.bootstrap_dev.dev_prices`); the config = platform.example.yaml + `t_beta` (account 444455556666) +
   `platform.cost.prices_ref: db:<n>`, proposed by A, approved by B (re-proposed when the recorded catalog differs),
   so usage lines cost more than 0 and are not `cost_estimated`; per tenant the ExternalId,
   the IAM role `AghooBedrockInvoke` in the tenant account (moto, trusting `noema-api` and `aghoo-jobs` of
   123456789012 with the ExternalId), projects `main` and `second`, principals `developer`, `developer2`,
   `tenant_admin`, `reviewer`, `end_user`, `auditor` (main) and `developer` (second), one `ak_test_` key each, every
   scope.
4. The state file `~/.noema/stacks/<project>.json` (0600, directory 0700; refused inside the repo): `endpoints`
   (`api` list, `metrics`, `mockbedrock`, `moto`, `postgres`, `redis`, `toxiproxy`), `operators.<name>.{operator_id,
   key_id,key}`, `tenants.<t>.{aws_account_id, external_id, invoke_role_arn, projects.<p>.{project_id,
   principals.<role>.{principal_id, subject, roles, key_id, key}}}`, `prices.{version, prices_ref, digest}`,
   `config.{version, digest, prices_ref}`, `host_env`; plus
   `<project>.env` (`set -a; . ~/.noema/stacks/noema-e2e.env`): the environment of a host-run process against the
   stack (endpoint overrides on the published moto/mock ports, `NOEMA_DB_DSN_REF=noema/db-dsn-host`, Valkey, fake
   AWS keys), e.g. `set -a; . ~/.noema/stacks/noema-e2e.env; make api-serve API_PORT=28199`.

## 7. The e2e stack (`make/e2e.mk`)

| Target | What |
|---|---|
| `make e2e-up` | `e2e-images` (skip with `E2E_BUILD=0`), base services + toxiproxy + mockbedrock, bootstrap, the app profile `--wait`, bootstrap again (records the api endpoints) |
| `make e2e-bootstrap` | seed or re-check the running stack |
| `make e2e-smoke` | `e2e-up`, then `pytest tests/e2e/smoke -m e2e` (`NOEMA_E2E_REQUIRE=1`) |
| `make e2e` | `e2e-up`, then the whole tree `pytest tests/e2e -m e2e` (smoke + the full suite, §10) |
| `make e2e-down` | stop everything and delete the volumes (database, spool) |
| `make e2e-ps`, `make e2e-logs [SERVICE=api-1]` | status, logs |

Variables: `E2E_PROJECT` (noema-e2e), `E2E_ENV` (dev: one Redis namespace for every process and Prometheus
metrics), `E2E_PORT_BASE` (28100; 0 = ephemeral), `E2E_VALIDATE` (set 1 for the server-side contract validator),
`E2E_BREAKER_TTFT` (`off`: the stack's breakers run without the p95-TTFT rule, since the scenarios change a model's
latency up to 30x between tests; `on` to arm it), `E2E_DB_HOST`, `E2E_STATE`, `E2E_TESTS`. Ports at the default base: api-1 28101, api-2 28102, metrics 28111/28112,
postgres 28132 (postgres/noema-dev-only), moto 28150, toxiproxy API 28174 (fault proxies 28175-28177), valkey 28179,
mockbedrock 28180. A second stack needs its own `E2E_PROJECT` and `E2E_PORT_BASE` (e.g. noema-e2e-ui, 28200).

Fixtures (`tests/e2e/conftest.py`): `stack` (session; the state file, both replicas ready; skip without a stack,
fail with `NOEMA_E2E_REQUIRE=1`), `stack.client(replica, tenant=, project=, role=)` -> `CheckedClient`
(`request(method, path, json=, expect=, **httpx)`, `sse(path, json=)`): every response is validated with the
`ContractValidator` (operation from the bundle by method and path; decision invariants paired with the request
JSON; SSE frame by frame); `mock` (control API `load`, `requests`; restores the default scenario after a test that
loaded its own). Smoke (`tests/e2e/smoke/test_smoke.py`, 8 tests): healthz/readyz on both replicas; one turn over
SSE on api-1 read back on api-2 (plus the t_beta 404); `/v1/decisions` native and Jev; an upload to its raster and
thumbnail derivatives through media-worker and media-sandbox; `GET /v1/usage` as developer and tenant_admin.

## 8. Tests

`tests/unit/composition/` (no network): every field real in each env, the shared instances, `wrap_adapters` reaching
the engines, the env refusals (`NOEMA_BREAKER_TTFT_RULE=off` outside dev/test included), a real build in a fresh
interpreter without `tests`; start/stop ordering, the drain bounds per process (virtual time), a failing start undone;
the SIGTERM chain delivered in an empty context with sniffio importable (`test_sigterm.py`: a drain hook runs,
readyz goes 503, the previous handler runs, stop() does not wait out the bound); the loop-lag gauge in every process,
a raising loop restarted with backoff, the per-process `service.name`, the DSN re-read after a failed pool creation
(`test_background.py`); `ActiveConfigRegistry` (empty, loads, notifies, keeps the
current snapshot on a rejected version, DB down); the entrypoints' kinds parsing, dispatch and HANDLERS merge.
`tests/integration/composition/test_boot.py`: `create_app()` with no injected services against a seeded in-process
moto, a scratch database and an in-process mock (bootstrap tool, real adapters, STS broker, authenticator, decision
engine, usage, tenant isolation), and the jobs entrypoint running the `session.delete` the api enqueued.
`tests/integration/infra/test_compose.py` / `test_images.py`: the app profile, the scenario mount, the fault routing,
the image CMD and the composition in the images.

## 9. Decisions and known limits

- `composition.py` stays one module (every module doc points at it); helpers are private classes there.
- New process variable `NOEMA_CREDENTIALS_BROKER` (dev/test `ambient` for the live smoke); not in spec-05 §5 yet.
- New process variable `NOEMA_BREAKER_TTFT_RULE` (`on` default; `off` dev/test only, the e2e/UI/perf stacks on the
  wire mock); not in spec-05 §5 yet (requests/integrator.md).
- New process variables `WEB_CONCURRENCY` (api workers per pod, §5.1) and `NOEMA_MAX_TURNS_PER_PROCESS` (the
  createTurn cap, read by `create_app`); not in spec-05 §5 yet (E sprint B).
- Trace export (Wave E): `build()` passes `otlp_traces_endpoint(environ)` to `setup_telemetry`: the standard
  `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, else `OTEL_EXPORTER_OTLP_ENDPOINT` + `/v1/traces` (OTLP/HTTP, batch
  processor). Unset (the compose stacks), no process exports spans and the provider samples nothing (`ALWAYS_OFF`
  since E sprint B, non-recording spans with valid ids for the log `trace_id`), which took ~2/3 of the span cost off
  every request; set, recording is back on: budget ~15-30 us of CPU per span (reports/perf/e-sprint-b.md).
- DSN rotation: `_DbPool` keeps the DSN of the pool it created. A rotated `noema/db-dsn`, or a fault-routing
  re-bootstrap with `--db-host toxiproxy:15432` on a running stack, reaches a running process only through a restart
  of its pods (a failed pool creation re-reads the secret, so a pod that never connected heals by itself).
- `aghoo_circuit_state` moves only on a call: an open breaker shows 2 until the first `admit` after `open_s` turns
  it half-open (lazy, adapters). Tooling that waits for a closed circuit must keep sending calls (the e2e heal does).
- The real authenticator ignores `NOEMA_DEV_API_KEY` (warning): keys come from noema-admin / the bootstrap.
- Test-env processes keep per-process Redis namespaces, so multi-process stacks run `NOEMA_ENV=dev` (`E2E_ENV`).
- No turns are attempted before the config loads: `/readyz` is 503 and requests fail with a retryable 500 until
  `config approve`; the pod heals within 5 s of the activation (or at once through `config_changed`).
- The default scenario has no rule for labeler prompts: sampled `labeling.item` jobs (2 % uniform) fail and retry.
- `make e2e-down` deletes the stack's volumes; the state file then no longer matches and the next bootstrap
  re-seeds from scratch (the old file is overwritten).
- moto keeps its state in memory: a restarted moto container loses the peppers, the DSN secrets and the ExternalIds
  while Postgres keeps the seeded rows, so the bootstrap refuses (`bootstrap` is one-time). Recover with
  `make e2e-down && make e2e-up`.

### 9.1 Wave E edge fixes in the API layer (`noema.api`, the ingress and the createTurn/decision routes)

- Ingress order: the config snapshot is pinned once the route is known, so `/healthz` (200) and `/readyz` (503)
  answer before the first config load and an authenticated operation gets the typed retryable 500 with its
  `x-request-id` (before: Starlette's plain-text 500 for every request). Only what the headers declare (a
  `Content-Length` over `max_json_body_bytes` -> 413, the Content-Type -> 415) is decided before authentication; the
  body is read after it, so a caller without a valid key makes the process buffer nothing (an undeclared-length
  body over the cap is now 413 after authentication, a 401 without a key). The raw bytes are dropped once the gate
  parsed them (routes read `ctx.json`; `RequestContext.body` is gone), and a body over 1 MiB (`OFFLOAD_BYTES`) is
  parsed, schema-checked and fingerprinted in a worker thread.
- Errors: an `AdmissionRejected` 429 sends its six x-ratelimit-* headers with the envelope (spec-08 §4). A replay
  sends the retry's own `x-request-id` (not the stored one), except on a stored error envelope.
- Access log: the JSON line's message is `<method> <route> <status> <ms>ms request_id=<id> key_id=<id>`, plus
  `tenant` and `event: http.access` (the JSON formatter keeps only the 08 §7 keys, so the spec-09 §2 fields were lost).
- Client IP: an IPv4-mapped peer or hop (`::ffff:a.b.c.d`, a dual-stack listener) counts as its IPv4 address.
- createTurn: without a valid `traceparent` the turn's trace id is `deps.trace_id` (the request's trace, else
  derived from the request id, as `/v1/decisions` does), never the invalid all-zero id. A keyed `stream: true` turn is
  marked dispatched once it started, so a retry after a drop attaches instead of running a second turn.
- Idempotency (`ops-ledger.md`): the stored fingerprint is bound to the principal (another principal's reuse of a
  key is 409, never a replay); a JSON response whose content the request may not keep (`store: false`, or a data
  class whose `store_content` is not `full`) is not stored, so a retry runs again.
- The per-process cap (§5.1) is tenant-fair and also covers createDecision and the facade.

## 10. The full e2e suite (`tests/e2e/suite`, `make e2e`; e2e author, Wave C2b/D)

`make e2e` = `e2e-up`, then `pytest tests/e2e -m e2e` (smoke + suite, 52 tests, about 2.5 min, serial: the tests
share one stack and one mock; `NOEMA_E2E_REQUIRE=1`). Against another stack: `make e2e E2E_PROJECT=noema-e2e-suite
E2E_PORT_BASE=28300 [E2E_BUILD=0]`. Every test loads its own mock scenario (`scenarios.py`: the golden fixtures'
scenarios with real-time margins of >= 200 ms between first text, S1, the 400 ms hold cap and the 1,000 ms router
deadline) and uses a unique input (`kit.tag()`, no L2 hit across tests). Every Noema response is validated:
`CheckedClient`, live SSE streams frame by frame and whole (`kit.live_turn`), the openai Python client through an
httpx response hook, the openai npm client by recording each response in its `fetch` (`facade_openai.mjs`) and
validating it in Python; turn streams also pass P-SSE-1 (`tests.golden.compare.sse_invariants`).

| File | Scenarios |
|---|---|
| `test_turns_sse.py` | G01 agree; G02 disagree + invisible restart (no A1 delta reaches the client); G03/G03b holdback_deadline then after_commit (decided spec agrees / differs: no restart, `served_spec_differs`); G04 reasoning route (sticky opus-deep, commit at Opus's first text); G05 pre-commit 503 -> in-profile fallback to Sol; G06 race (lost_race) |
| `test_turns_control.py` | G07 cancelTurn on api-2 for a turn streaming on api-1 (`cancelling`, then `cancelled`); G08 verification after `injection_suspected` (bindings[4]); G09 every decision call 503; G10b stream exception after commit (`all_routes_unavailable`); G10 stall after commit (`upstream_stalled` at the 30 s idle timer, slow); G11 `store: false` (getTurn 404 on both pods, `last_seq` unchanged) |
| `test_turns_attach.py` | G12: the idempotent duplicate on api-2 attaches to api-1's running turn (identical frames, no model call), R3 replay after completion, R5 409 `idempotency_key_reused` |
| `test_decisions.py` | native ensemble (Mantle + Converse members, getDecision on the other pod), Jev, escalation lightweight-model-decide -> router-default (`margin_below`), hierarchical 32 options (reproduces `decision-result.valid-hierarchical.json`), L2 hit across replicas |
| `test_facade_sdks.py` | `openai.AsyncOpenAI` and the `openai` npm client with `base_url = <api>/openai/v1` (Noema, not OpenAI): example answers, `x-aghoo-include: ensemble`, unavailable -> refusal, 400 `model_not_available`, 403 `profile_not_allowed` |
| `test_sessions.py` | expected_seq 409 `session_conflict`; FIFO queue across replicas (served after the holder; 409 `session_busy` past `queue_timeout_ms`); listSessions / listSessionTurns paging and a tampered cursor |
| `test_files.py` | PDF with OpenAction + document JavaScript: `pdf_sanitized` clean, markdown, page image, and the turn's `active_content_removed` degradation; HEIC -> PNG raster + thumbnail; encrypted PDF and zTXt decompression bomb rejected; download headers (`Content-Disposition: attachment; filename="<file_id>-<kind>[-<page>].<ext>"`, `nosniff`) |
| `test_usage.py` | the lines of a turn and a decision under every `group_by` (same totals per dimension), the turn's generation line priced by the dev catalog (`cost_usd > 0`, not estimated), paging, `from`/`to`, the 366-day limit (t_beta `second`) |
| `test_lifecycle.py` | deleteSession: `session.delete` on the jobs process removes the session's turn and file and purges its objects from the bucket; deleteFile: `file.purge` on the media-worker (reads stop at once, then the objects go); a dropped client connection is not a cancel (the whole text is stored); resumeTurnEvents 501 `not_implemented` on both pods (IMPL-036) |
| `test_turn_inputs.py` | inline `input_image` through `ingest_inline` (image modality, the raster bytes reach Sol); a low-confidence intent (bindings[1]): `cover_top2_and_clarify`, the signed `<decision_context>` block as the committed attempt's last text block (signature recomputed from the wire body) and the forged tags of the user text escaped (`&lt;`); `wait_for_files` on a PDF still being processed (its markdown and page image reach Sol); http(s) `image_url` refused with 400 on the facade and createTurn, no model call |
| `test_shutdown.py` | SIGTERM to api-1's container during a turn (docker CLI, the stack's compose project): `/readyz` 503 at once, the turn streams to its end, stored, its usage line flushed, the process exits well before the 45 s grace; the container is started again and api-1 is ready before the next test (slow) |
| `test_multipod.py` | Haiku 503 burst on api-1 opens its breaker, api-2 follows through `breaker_events` (`circuit_open`, no Haiku call), healed in `finally` (`heal_circuits`); t_beta `rpm_per_key: 4` through noema-admin (propose A, approve B, rollback + approve after): one GCRA bucket for both pods (remaining 3, 2, 1, 0, then 429 on either); the per-IP auth-failure bucket shared; a revoked key refused by both pods within 5 s (cache TTL 60 s) |
| `test_authz.py` | 28 role / project / tenant rows (developer2, tenant_admin, auditor, reviewer, end_user, the second project, t_beta, anonymous) |

Helpers: `kit.py` (live SSE turns, polling, the mock's request log, `admin()` = noema-admin in a subprocess with the
stack's dev environment, `/metrics` samples), `turns.py` (validated turn, stored turn, an opus-deep session),
`multipod.py` (keyed clients, minted keys, `patched_config`, breaker state, `heal_circuits`), `conftest.py` (before
each test: the default scenario, then `heal_circuits`: every `aghoo_circuit_state` closed or absent on both replicas,
probing each replica that shows a tripped circuit once a second (a router-default decision for the members, a
`stream: false` turn otherwise) and failing after 45 s with the circuits still open). The breakers outlive a test and
both pods share them through `breaker_events`, so without it G09 or the multipod burst left the next tests'
members `circuit_open`; `make e2e-up` also disarms the p95-TTFT rule (`E2E_BREAKER_TTFT=off`), which the scenarios'
30x latency changes between tests tripped at random (C2b/D review finding 1). Media fixtures come from `tests/unit/media_pipeline/media_fixtures.py`.

Decisions and limits:
- Timing assertions are orderings and reasons, never exact milliseconds. What depends on member timing is asserted
  in its invariant form: whether `injection_suspected` is in S1 (G08: the stored `matched_bindings` has both), and
  whether S1 is final (G09: every snapshot unavailable). A call the engine gives up on is `disconnected` in the mock
  log. Sampled `labeling.item` jobs may add labeler calls (Sol, Sonnet; never streams) to any test's window, so
  mock-log assertions filter by model or op.
- The GCRA test mints a fresh key per run (its bucket stays drained about 60 s) and restores the config in
  `finally`; the bootstrap's recorded config version stays the active one (`config rollback` re-activates it).
- The auth-failure test drains this host's per-IP bucket and sleeps 6 s (10/s refill) before the next test.
- The npm facade script refuses to start unless `NOEMA_E2E_FACADE` is a local `http://127.0.0.1` / `localhost` URL,
  and refuses any request outside it (node subprocesses are not covered by pytest-socket; the openai client would
  otherwise fall back to OpenAI's public endpoint).
- Not covered here (Wave E): G12 R4 and the abandon watchdog (both need `resume_window_s` = 300 s of idle stream),
  Last-Event-ID resume (resumeTurnEvents is 501 in Phase 1, IMPL-036; the mirror's cross-pod path is G12), labeling
  end to end (the default scenario has no labeler rule; sampled items fail and retry), the full authz matrix, the
  fault suite, Redis/Postgres outages during a turn and a slow client (`client_too_slow`).
