# Architecture (as built, Phase 1: tag `wave-e3` + the Wave F working tree, untagged)

This describes what the code does. The design it implements is the design repo `/codes/aghooai/docs/noema`
(`../docs/noema` from the repo root; revision 6 at the start of the build, revision 7 records the build). The
implementation decisions are [IMPL-DECISIONS.md](IMPL-DECISIONS.md) (ids `IMPL-0nn`), the per-area specs are
[impl-spec/](impl-spec/README.md) (`spec-0N`), and each module's handoff doc is in [modules/](modules/). Where this
file and a spec differ, the code and its tests decide; report the difference.

## 1. Overview

One request path runs two engines concurrently. The **decision engine** asks an ensemble of small Bedrock models
typed questions about the turn (intent, complexity, risk; or the client's own questions on `/v1/decisions`). The
**generation engine** starts the answer at once on a speculative spec (spec0). When the commit-critical answers
are in (S1), the **bindings** turn them into the decided spec (generation profile, effort, output budget, answer
mode, conditioning). The running attempt commits if its spec equals the decided spec; otherwise it is cancelled
and restarted invisibly. The client sees only the committed attempt's text.

```text
 client ──HTTPS──> api process (uvicorn, 2 per pod) ──SigV4 / bearer──> Bedrock in the TENANT's account
                     │  ingress: edge shed, auth, gate, cap, authz, idempotency, admission       (STS AssumeRole
                     │  turn: decision run ‖ generation attempts → commit → SSE                   + ExternalId)
                     ├── Postgres 16 (RLS)   ── Valkey 7.2 (buckets, pub/sub, event mirror, outbox stream)
                     ├── S3 (per-tenant prefix, SSE-KMS)   ── Secrets Manager (peppers, DSN, ExternalIds)
 jobs process ───────┘  labeling, session.delete, retention, reaper, notify bridge
 media-worker ──UDS──> media-sandbox (network none, nsjail per job): sanitize, extract, OCR, rasterize
```

## 2. Processes

| Process | Entry point | Image | Runs |
|---|---|---|---|
| `api` | `uvicorn --factory noema.api.app:create_app --loop uvloop --http httptools` (image CMD; `WEB_CONCURRENCY=2` workers per 2-vCPU pod) | `noema-api` | HTTP and SSE, orchestrator, both engines, adapters; `/metrics` on `NOEMA_METRICS_PORT` + worker index |
| `jobs` | `python -m noema.entrypoints.jobs --kinds labeling.*,session.delete,retention.sweep` | `noema-worker` | job handlers with tenant STS, periodic-job scheduler, job reaper, notify bridge (`pg_notify` to Redis) |
| `media-worker` | `python -m noema.entrypoints.media_worker --kinds media.process,file.purge` | `noema-worker` | the spooler for `media.process` and `file.purge`; no parser libraries |
| `media-sandbox` | `python -I -m noema.media.sandbox.sandboxd` | `noema-media` | the jailer: one nsjail per extraction job; `network_mode: none` |
| `noema-admin` | `python -m noema.identity.cli` | any (venv or image) | the operator CLI (IMPL-044); its own `noema_cli` connection, never the composition |

Every process builds its `Services` with `composition.build()` (sync, no I/O, validates the process environment)
and `composition.start(services, process)` (opens clients, spawns the background tasks below, installs the
SIGTERM chain). Background loops run under a supervisor that restarts a failing loop with backoff 1 to 30 s.

| Background task | api | jobs | media-worker |
|---|---|---|---|
| config watcher (`config_changed` pub/sub plus a 5 s poll of the active `platform.config_version`) | x | x | x |
| event-loop lag sampler (`aghoo_event_loop_lag_seconds`), datastore pool gauges | x | x | x |
| breaker and retry-budget sync (pub/sub, 1 s) | x | x | |
| outbox writer; outbox drainer | x | x | writer |
| calibrator registry refresh (`calibrators_changed` plus a 5 s poll) | x | x | |
| key-revocation listener (`keys_revoked`); running-turn registry, cancel listener, abandon watchdog | x | | |
| periodic-job scheduler, job reaper (30 s), notify bridge | | x | |
| job worker | | x | x |

Source: [modules/integration.md](modules/integration.md) sections 3 and 4, spec-01 §3-§4.

## 3. Module map and layers

| Package | Owner role | Contents |
|---|---|---|
| `core` (frozen) | platform | ids, `Clock`/`Deadline`, `ErrorCode` and the one-code-one-status `ERROR_TABLE`, `Principal`, value types, `RedisKeys`, `ProcessSettings`, every Protocol in `core/protocols/` |
| `contracts` (frozen) | platform | byte copy of the design contracts (`_vendor`), the bundled OpenAPI, generated strict models, `RequestGate`, the test-mode `ContractValidator` |
| `config` (frozen) | platform | typed config models, loader (schema, defaults, checker, P1 rules, price catalog, digest), registry |
| `db` (frozen) | db-schema, db-repos | Alembic revisions, roles, `uow(principal)` with per-transaction `app.tenant_id`, definer functions, `repos/` (the only SQL) |
| `api` (frozen; routes by owners) | api | app factory, middleware (request id, trace, body cap, auth, gate, idempotency, errors), SSE framing, edge shed and per-process cap, routes |
| `identity` | identity | API keys, authn, authz matrix, STS broker (ADR-001), secrets, sessions service, `noema-admin` |
| `adapters` | adapters | Converse/ConverseStream and Mantle chat adapters, capabilities, error classification, breakers, retry budget, timeouts |
| `decision` | decision | question compiler, members (logprob, verbalized), backups, pooling, confidence, escalation, hierarchical method, Jev and facade mapping, L2 cache |
| `generation`, `streaming` | generation | attempt FSM, `single`/`fallback`/`race`, holdback buffer, cancel estimates; per-turn event log with a Redis Streams mirror, SSE writer with backpressure |
| `turns` | orchestrator | the turn orchestrator, spec, bindings, conditioning, late policy, verification, session ordering, preflight, context, prompt rendering and spotlighting |
| `media` | media-pipeline, media-negotiation | intake, quarantine, pipeline, sandbox backends, extractors, worker; `negotiation.py`, `budget.py`, `views.py` |
| `ops` | ops-admission, ops-ledger | `admission/` (GCRA Lua, work graph, fair queue); cost, prices, usage, jobs, outbox, idempotency, retention, reapers, notify bridge |
| `labeling` | labeling | sampler, D4 labelers, temperature and Platt calibrators, acceptance gate, registry |
| `telemetry` (frozen) | fakes-telemetry | OpenTelemetry setup, the metric and span registry, redaction |
| `composition.py`, `entrypoints/` | integrator | the wiring of every `Services` field; the worker entrypoints |

Import layers are enforced by import-linter (`make importlint`, `.importlinter`; 27 contracts kept, 0 broken on the
final tree, [reports/qa/wave-e-summary.md](../reports/qa/wave-e-summary.md) section 4):
1. `core` imports nothing else from `noema`; `contracts`, `db`, `telemetry` import only `core`; `config` imports
   `core` and `contracts`.
2. Domain packages never import each other. They meet through `core.protocols` and `core` value types, with
   implementations injected by `composition.py`. Two split packages carry extra independence contracts
   (`media.{negotiation,budget,views}` against the pipeline modules; `ops.admission` against the ledger modules).
3. A route imports `api.deps`, `core`, `contracts`, `config` and only its owner's domain package.
4. Only `api.app`, `api.deps` and `entrypoints` import `composition`.
5. The heavy media libraries (Pillow, pikepdf, Docling, torch, pypdfium2, pytesseract, magika, libmagic,
   markdown-it) are imported only by `noema.media.extractors`. The api and worker images do not install them, so
   a violation also fails at import time.
6. Forbidden everywhere: `openai` (used only by one facade conformance test), `anthropic`, `requests`, `boto3`.
   `make forbidden-scan` also fails on `api.openai.com` in the source trees.

## 4. Request lifecycle

### 4.1 Ingress (every POST; spec-04 §1, frozen order)

| Step | What | Refusal |
|---|---|---|
| edge shed | `noema.api.shed`, before the middleware stack: an unkeyed createTurn, createDecision or facade `POST` from a tenant the per-process cap would refuse, recognised by its key id in the in-process key cache (secret not verified) | 429 `queue_timeout`, `retry-after: 1` |
| 0-2 | pin the config snapshot; request id (echo a valid `x-request-id` or `req_<ULID>`); trace context | - |
| 3 | declared `Content-Length` over `platform.limits.max_json_body_bytes` (50 MiB); content type | 413 `request_too_large`, 415 |
| 4 | authenticate the API key (HMAC-SHA256 with the versioned pepper, LRU key cache); then the body is read | 401 `invalid_api_key` (an undeclared-length over-cap body without a valid key is 401, not 413) |
| 5 | request gate: JSON Schema and request invariants, inline image size by arithmetic, at most 64 media parts; then the authoritative per-process cap (`NOEMA_MAX_TURNS_PER_PROCESS`, default 120) | 400 `schema_violation`, 413 `image_too_large`, 429 `queue_timeout` |
| 6 | authorize (role and scope matrix, session owner) | 403 or 404 |
| 7 | idempotency `begin` (keyed POSTs): replay, attach to the running turn, conflict, or new | 409 `idempotency_key_reused` |
| 8-11 | turn plan and question compile; tenant credentials (`broker.get`); session lock; token estimate and `Admission.reserve` of the whole work graph (every decision call, backup, attempt, verification) | 4xx per plan error; 503 `tenant_access_misconfigured`; 409 `session_busy`; 429 `rpm_exceeded` / `tpm_exceeded` |
| 12 | start the turn task, register the turn owner, send the SSE headers, emit `turn.created` | - |

### 4.2 A turn

1. **Preflight** (no model calls): media inventory and views, wait for uploads (`wait_for_files`), context
   selection (recent turns within budget, IMPL-045).
2. **Concurrently**: the decision run (the router profile's questions, one call per member and question, backups at
   `backup_after_ms`) and attempt A1 on spec0 (the sticky or default spec), whose text is held in the holdback buffer.
3. **S1** = every commit-critical answer final (or the decision wait limit). The bindings give the decided spec.
4. **Commit** at `max(S1, first text)` if A1's spec equals the decided spec (`generation.committed{spec_agrees}`);
   otherwise A1 is cancelled (`spec_changed`) and A2 starts on the decided spec. The hold cap (400 ms) can commit
   A1 before S1 (`holdback_deadline`). Pre-commit failures fall back within the profile; a turn has at most 2
   failure attempts (IMPL-016).
5. Deltas of the committed attempt only; non-blocking answers arrive in at most one `decision.updated`
   (IMPL-002); verification runs post-commit when a binding or `verify: true` asks (IMPL-024).
6. **Completion** (`store: true`): one transaction writes the turn with the next session `seq`, every attempt and
   the decision rows, within a 5 s budget; if Postgres fails, the finalize record goes to the write-behind outbox
   and the session stays busy until it is applied. Usage lines and audit rows always go through the outbox
   (IMPL-022). `turn.completed` is the last event.

The event order is IMPL-012 (spec-04 §4). `POST /v1/decisions` runs the decision engine alone (escalation allowed,
IMPL-025/068; the hierarchical method above the flat limit, IMPL-027). The OpenAI-shaped facade maps onto it.

### 4.3 Shutdown (spec-11 §7)

SIGTERM: `/readyz` 503 at once and the drain hooks start; uvicorn stops accepting `NOEMA_DRAIN_DELAY_S` (5 s)
later; running turns get 30 s, then end with a retryable `all_routes_unavailable`, and 5 s to finalize; the outbox
is flushed (10 s); pools close. The image runs uvicorn with `--timeout-graceful-shutdown 35`; compose gives the api
45 s and the workers 35 s. Workers stop claiming and give running handlers 10 s; unfinished jobs return through
the reaper.

## 5. Data stores

| Store | Holds | Isolation |
|---|---|---|
| Postgres 16 | schemas `noema` (tenant data: sessions, turns, attempts, decisions, files, derivatives, usage lines, idempotency records, audit, labeling, calibrators), `auth` (API keys), `jobs` (`jobs.job`, SKIP LOCKED), `ops` (outbox dead letter), `platform` (config versions, price catalog, operators), `admin` (CLI functions) | roles `noema_owner` (migrations), `noema_app` (no BYPASSRLS), `noema_cli`, `noema_definer` (definer functions only); `tenant_id` on every tenant row with ENABLE + FORCE RLS on `current_setting('app.tenant_id')` (IMPL-009, spec-03) |
| Valkey / Redis | GCRA buckets and stream slots (admission, per-IP auth failures), L2 decision cache `dec:l2:<tenant>:<digest>`, per-turn event mirror (Redis Streams), turn owners and attach, session queues, idempotency done signals, outbox streams `outbox:<0..7>`, pub/sub (`config_changed`, `keys_revoked`, `calibrators_changed`, breaker events, `turn_cancel`) | every key through `RedisKeys`; L2 keys and digests are per tenant (HMAC with `noema/input-digest-pepper`); pub/sub payloads carry `tenant_id` and are dropped on mismatch |
| S3 | bucket `noema-<env>`: uploads, derivatives, idempotent bodies over 1 MiB under `<tenant_id>/idempotency/<record_id>` (IMPL-085) | per-tenant prefix; SSE-KMS `alias/noema-<env>-<tenant_id>` in staging and prod, AES256 in dev and test |
| Secrets Manager | `noema/api-key-pepper`, `noema/input-digest-pepper`, `noema/db-dsn` (the `noema_app` DSN), `platform/tenants/<tenant_id>/external-id` | platform account; read by the processes, never logged |

Postgres is the system of record. Valkey holds caches, buckets and coordination, plus outbox records while
Postgres is unavailable; a Redis outage keeps turns serving (local buckets, resume disabled; fault F10 PASS,
[reports/qa/wave-e-summary.md](../reports/qa/wave-e-summary.md)). Postgres down: `store: true` turns fail
fast with a retryable 500 and `store: false` turns continue (F11 PASS).

## 6. Trust boundaries (design 02 §2.2; spec-09 §1)

| Boundary | Phase 1 control | Evidence |
|---|---|---|
| Z0 clients to Z2 (no Z1 edge, WAF or BFF in Phase 1) | 50 MiB body cap, upload caps, 64 media parts; 256-bit API keys, uniform 401, per-IP GCRA on 401s; the principal only from the key row | SE12, SE15 |
| Z2 internal | authz matrix plus RLS; principal-bound idempotency fingerprints | SE1 (356 cells, 0 leaks), SE2 |
| Z2 to Z3 (media sandbox) | network-less container, nsjail per job, seccomp, rlimits; only listed re-encoded outputs leave | SE9 |
| Z2 to Z4 (stores) | RLS, per-tenant HMAC digests and L2 keys, per-tenant KMS keys | SE2, SE3 (L2 half) |
| Z2 to Z7 to Z6 (STS, the tenant's Bedrock) | one STS session per (pod, tenant), ExternalId, ARN allowlist, account asserted on every call, fail closed (ADR-001) | SE4 (moto) |
| Z6 to Z2 (model output) | outputs are data; distributions parsed and range-checked; `reasoningContent` never forwarded; escaped `<decision_context>`; datamarked documents and OCR text (IMPL-086) | SE6, SE7, SE11 |
| Z5 to Z2 (config) | two-person `noema-admin config propose` / `approve`, schema plus checker plus P1 rules at propose and at load, audit rows | SE15, e2e `test_multipod.py` |

Statuses are in [reports/qa/wave-e-summary.md](../reports/qa/wave-e-summary.md) section 2. The threat model summary
for reporters is in [SECURITY.md](../SECURITY.md).

## 7. Key design decisions

| Id | Decision (as built) |
|---|---|
| IMPL-002, IMPL-012 | Inside turns `quorum` marks the snapshot sent while a non-blocking answer is still open; exactly one `decision.updated` follows. The commit point is `max(S1, first text)`; `decision.completed` is always emitted. Early commit stays off (R5) |
| IMPL-007 (ADR-001) | "AssumeRole per call" (D2) is read as per-call tenant credentials from one cached STS session per (pod, tenant): 3,600 s sessions, soft refresh at expiry minus 600 s, hard at minus 300 s, single flight, 10 s negative cache, fail closed |
| IMPL-008, IMPL-044 | API keys `ak_{live,test}_<key_id>.<secret>`, stored as HMAC-SHA256 with a versioned pepper; minted only by `noema-admin`, printed once. Operators use `op_` keys; config approval and calibrator activation need two operators |
| IMPL-009 | `tenant_id text` on every tenant row, FORCE RLS, SECURITY DEFINER functions with a fixed `search_path` for the pre-tenant and cross-tenant paths |
| IMPL-016 | A fallback to another model is not a retry (R6). The retry budget gates same-model re-sends only: 10% of first sends over a sliding 60 s, no floor, counting only re-sends the upstream may have received (R8). At most 2 failure attempts per turn; no fallback on `tenant_access` |
| IMPL-021 | The per-turn event log lives in process with a Redis Streams mirror for the 5-minute resume window; `resumeTurnEvents` is 501 in Phase 1; an idempotent replay of a streamed turn after the window is 410 `resume_window_expired` |
| IMPL-022 | Ordering-critical writes are synchronous (session lock before admission); billing and audit go through a write-behind outbox (Redis stream fallback, dead letter after 10 deliveries) |
| IMPL-025, IMPL-068 | No escalation inside turns with the shipped deadlines; `/v1/decisions` escalates (`router-plus`), and request questions are always eligible |
| IMPL-027, IMPL-036 | The hierarchical choice method is built for native `/v1/decisions` and turn app questions (owner answer 2026-10-10); the facade and `output_format: "jev"` answer 422 `unsupported_parameter`. Out-of-phase values are 422 `feature_not_available` or 501 |
| IMPL-031 | `profile` is optional on `/v1/decisions`; the default is the tenant's `defaults.decision_profile`, filled with `lightweight-model-decide` (D6) |
| IMPL-050 | Decision member calls are never retried; backups are time-triggered. The Converse and Mantle transport is lean (own SigV4 signer and eventstream decoder, byte-identical to botocore) on the same wire |
| IMPL-064 | 50 MiB JSON body cap (owner answer 6), at most 64 media parts, authentication before the body is read (R7) |
| IMPL-072 (ADR-003) | Calibrators are bound to (profile version, signature, member subset, input-view signature); lifecycle `fitted`, `accepted`/`rejected`, `active`, `downgraded`/`retired`; two-person activation; drift downgrades a platform artifact for every tenant |
| IMPL-077, IMPL-085 | Idempotency records store a principal-bound fingerprint; a JSON response is stored only if a model call was dispatched and its content may be kept; bodies over 1 MiB go to S3, so size never re-bills |
| IMPL-078 | Hot-path calls (`admit`, `consume`, `lookup`, `estimate`) are sync and in memory; breakers are shared across pods through pub/sub |
| IMPL-081 (ADR-002) | The media sandbox is a network-less container running nsjail per job (bwrap for dev, an rlimit runner for unit tests only) |
| IMPL-083 | Roles `tenant_admin`, `developer`, `reviewer`, `end_user`, `auditor`; scopes per key; 403 only where declared, otherwise 404 or an empty page |
| IMPL-086 | Spotlighting `standard` always datamarks document and OCR-derived blocks; `strict` also datamarks user text and history |
| IMPL-087 | 2 api processes per 2-vCPU pod; a tenant-fair per-process cap with 429 `queue_timeout`, checked at the edge and in the route; `gc.freeze()` and GC thresholds (700, 10, 100) |
| IMPL-088 | One bounded Redis pool per process (`NOEMA_REDIS_MAX_CONNECTIONS`, 0.5 s wait); pool gauges and sampled latency histograms for Postgres and Redis |
| IMPL-089 | The api and worker images run CPython 3.12.15 from python-build-standalone; the pods are sized from that image's container knee (owner acknowledgement pending) |

## 8. Deviations from design revision 6

Revision 6 (2026-10-10) is the design the build started from. What the build changed or refined after it, and
the record of each change:

| Area | Revision 6 | As built | Record |
|---|---|---|---|
| Pod sizing | 08 §9: 4 x (2 vCPU, 4 GiB) api pods, ~200 concurrent streams each | 2 api processes per 2-vCPU pod, at most **125 streams per pod** (the knee of the Wave F image measured in the container; 75 for the Wave E3 image), planned at 100: 8 pods for ~800 streams | IMPL-087, IMPL-089; owner decision 2026-10-12; [reports/perf/sizing.md](../reports/perf/sizing.md) §5 |
| L1 gate | p50 <= 10 / p95 <= 25 ms at 200 streams per pod | passes at the sized density (Wave F image at 125 per pod: p50 9.72 / 9.73, p95 14.06 / 13.98 ms; E3 image at 75: 9.65 / 12.38 ms); as written at 200 per pod p50 11.70 ms (E3, venv) fails | [reports/perf/sizing.md](../reports/perf/sizing.md) §6, [reports/qa/wave-e-summary.md](../reports/qa/wave-e-summary.md) L1 |
| Overload | no per-process limit | 429 `queue_timeout` with `retry-after: 1` above `NOEMA_MAX_TURNS_PER_PROCESS` (120), shed at the edge before authentication for cached key ids, max-min fair between tenants | IMPL-087 and its Wave E3 amendment |
| Ingress order | body cap before authentication | only header-declared size and type before authentication; the body is read after it (an over-cap chunked body without a key is 401) | R7, IMPL-064 |
| Media parts | no count limit | at most 64 media parts per request, 400 `schema_violation` | R7, IMPL-064 |
| Malformed ids | not specified | ids with U+0000 or control characters are 404 in paths, 400 in queries, never 500 | R7, IMPL-030 |
| U+0000 | not specified | U+FFFD in stored model and turn text; 400 `schema_violation` in client-supplied structured fields | R9, IMPL-022 |
| Retries | retry budget over all retries | fallbacks never spend it (R6); 10% of possibly-delivered first sends, no floor (R8) | IMPL-016 |
| Idempotency | stored responses | stored only if a call was dispatched and the content may be kept (a keyed `store: false` retry re-executes and bills); another principal's reuse is 409 | R7, IMPL-077 |
| Converse transport | aiobotocore | lean transport on aiohttp, own SigV4 signer and eventstream decoder, byte-identical to botocore (28/28 live requests, 4,304/4,304 messages) | IMPL-050 amendment; [reports/qa/wave-e-summary.md](../reports/qa/wave-e-summary.md) section 4 |
| Malformed 2xx | not specified | a 2xx Converse body that is not a Converse response is retryable `malformed:response` | IMPL-076 |
| Breakers | failure ratio and p95-TTFT rules | text-less stops count as failures; the p95-TTFT rule needs distinct tenants and can be disarmed in dev/test (`NOEMA_BREAKER_TTFT_RULE=off`) | IMPL-078 |
| Redis outage | resume disabled | a cross-pod attach waits, then 410 `resume_window_expired` | IMPL-021 |
| Hierarchical answers | a dominant one-option group gave `unavailable / no_quorum` | the expanded set holds at least 2 options, so that group gives a scored answer | R1, IMPL-027 (d) |
| Token estimator | chars / 4 for every model | a per-model chars-per-token table plus a per-request overhead (`TOKEN_ESTIMATOR_V1`), replaceable without a code change by `NOEMA_TOKEN_ESTIMATOR_TABLE`; the waste estimate of a cancelled generation stream uses the shipped table's chars per token (Wave F) | IMPL-029c, IMPL-020 |
| GC | not specified | `gc.freeze()` at startup, thresholds (700, 10, 100) | IMPL-087 |
| Object storage | 08 §5: a media bucket and KMS key in each tenant account | one platform bucket `noema-<env>` with a tenant prefix and a per-tenant KMS key `alias/noema-<env>-<tenant_id>` in the platform account (staging, prod); the tenant-account bucket is deferred to Phase 2 (ASR), pending the owner | IMPL-091 (material, owner review), spec-03 §2, spec-09 §6.7, `core/objects.py` |
| Operator proof | operator key checked in the CLI | the database verifies the operator key per connection (migration `0003_operator_proof`) | IMPL-090, IMPL-044, [modules/db.md](modules/db.md) |
| Live smoke | at most 60 calls | at most 87 ledger entries in total (85 used) | owner decision 2026-10-12, IMPL-080 |
| Process environment | spec-05 §5 list | adds `NOEMA_CREDENTIALS_BROKER`, `NOEMA_BREAKER_TTFT_RULE`, `WEB_CONCURRENCY`, `NOEMA_MAX_TURNS_PER_PROCESS`, `NOEMA_REDIS_MAX_CONNECTIONS`, `NOEMA_TOKEN_ESTIMATOR_TABLE` | spec-05 §5, [operations/deployment.md](operations/deployment.md) |

Rulings R2 (IMPL-012), R3 (IMPL-075), R4 (IMPL-078) and R5 (IMPL-072: the ECE noise floor and `DRIFT_MIN_N` =
100) clarify behavior without contract changes ([impl-spec/OWNER-CHECKPOINT.md](impl-spec/OWNER-CHECKPOINT.md)).
The rulings are orchestrator engineering calls; the owner decisions are D1-D6, R5 (early commit off), the answers
of 2026-10-10 and the decision of 2026-10-12.
