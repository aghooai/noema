# noema.ops ledger modules (owner: ops-ledger; Wave C1, adapted in C2a)

Spec: spec-11 §2-§7 (cost and usage, outbox, jobs, idempotency, retention, reapers, health), spec-02 §7-§8,
spec-03 (usage_line, idempotency_record, jobs.job, outbox_dead_letter), spec-05 §2, IMPL-019/020/022/040/077/085.
Packages: `noema.ops.{cost,prices,usage,jobs,outbox,idempotency,retention,reapers,notify_bridge}` and the routes
`noema.api.routes.{usage,health}`. They import only core, config, db and their own `noema.ops` siblings (never
`noema.ops.admission`; import-linter kept). Every constructor is sync (composition `build()` stays sync); the
async work happens in the `run()` loops that `composition.start` spawns.

## Public surface and wiring (composition.py)

| `Services` field / task | Construct | Notes |
|---|---|---|
| `estimator` | `HeuristicTokenEstimator()` (`noema.ops.cost`) | sync, pure (IMPL-029c) |
| `outbox` | `WriteBehindOutbox(db, redis, keys, clock, telemetry.instruments)` (`noema.ops.outbox`) | `put` sync O(1); `run()` = batch writer + `session_pending` release task; `flush(timeout_s)` |
| `cost` | `LedgerCostMeter(clock, config, outbox, telemetry.instruments)` (`noema.ops.cost`) | `price` sync; `record` -> `outbox.put` |
| `jobs` | `PgJobQueue(db, clock, ids, pod_id=settings.pod_id, listener=PgListener(app_dsn, clock), redact=telemetry.redact)` (`noema.ops.jobs`) | worker id `<pod>:<pid>`; `listener=None` = poll only |
| `idempotency` | `PgIdempotencyStore(db, redis, keys, BodyStore(s3, bucket=settings.s3_bucket, env=settings.env), clock, ids, config, telemetry.instruments, pod_id=settings.pod_id)` (`noema.ops.idempotency`) | owner `<pod>:<request_id>`; optional `poll_s=0.25` (tests only) |
| outbox drainer | `OutboxDrainer(db, redis, keys, clock, telemetry.instruments, consumer=settings.pod_id)` (`noema.ops.outbox`) | api, jobs |
| periodic scheduler | `PeriodicScheduler(db, clock)` (`noema.ops.jobs`) | jobs only |
| job reaper | `JobReaper(db, clock)` (`noema.ops.reapers`) | jobs only |
| notify bridge | `NotifyBridge(PgListener(app_dsn, clock), redis, keys)` (`noema.ops.notify_bridge`) | jobs only |
| job handlers | `noema.ops.retention.make_handlers(services)` -> `{"retention.sweep": ...}` | merge with the other owners' maps |
| routes | `noema.api.routes.usage.router` (getUsage), `noema.api.routes.health.router` (healthz, readyz) | already in `API_ROUTERS` |

`db` is the production `PgDatabase(engine, make_unit_of_work)`, `redis` the bytes client, `keys` the process
`RedisKeys`, `s3` the aiobotocore client (holder objects are fine: nothing is called before `start()`). `app_dsn` is
the noema_app DSN (`SecretStr`, from `settings.db_dsn_ref` via the secrets provider): `PgListener` opens its own
unpooled, role-checked LISTEN connection per `listen()` call (one for the job worker, one for the bridge). Every
`CHECK_S` (5 s) it runs `SELECT 1` on that connection (2 s bound) and reconnects when it fails, so a connection that
died without FIN/RST is replaced instead of looking idle forever (Wave E).

`start(services, process)`:
```python
# api and jobs (and media-worker if it puts outbox records)
runtime.spawn("ops.outbox.writer", outbox.run)
runtime.spawn("ops.outbox.drainer", drainer.run)
# jobs (media-worker: the worker only, with its own kinds and handlers)
handlers = merge_handlers(identity_jobs.make_handlers(s), labeling_jobs.make_handlers(s), retention.make_handlers(s))
worker = services.jobs.worker(expand_kinds(kinds, handlers), dispatch(handlers), concurrency=N)
runtime.spawn("ops.jobs.worker", worker.run)
runtime.on_drain("ops.jobs.worker", worker.drain)     # stop claiming, wait for running handlers (grace)
runtime.spawn("ops.jobs.scheduler", scheduler.run)
runtime.spawn("ops.reapers.jobs", reaper.run)
runtime.spawn("ops.notify_bridge", bridge.run)
```
`stop(services)` already does the spec-11 §7 order: drain hooks (grace) -> `outbox.flush(10)` -> close the runtime
(cancels the loops; a cancelled `Worker.run` gives running handlers 10 s, then leaves their jobs to the reaper) ->
close the clients. `/readyz` turns 503 as soon as `runtime.draining` is set (SIGTERM).

## Behavior summary

- **CostMeter** (spec-11 §2): catalog key `models.<alias>.price_ref` else the Bedrock id, plus `region`, which
  must be the tenant's `home_region` (spec-11 §2, IMPL-019), never the serving region: catalogs are keyed by home
  region, so a serving region finds no entry and the call is priced 0, estimated (request 6: decision, generation
  and labeling pass the tenant's `home_region`, verified in C2b); newest `effective_from <= now`. Decimal (28 digits), half-even to 1e-6 per
  call; writes use `cache_write_5m`. `estimated` = estimated usage, any non-zero component without a price
  (missing entry/field, "TBD"), or inconsistent usage (cache tokens above input: uncached clamped to 0).
  `price(alias, region, usage, *, snapshot=None)` prices with the catalog of `snapshot`, the caller's pinned config
  (IMPL-019, request 4: decision, generation and labeling pass `ctx.snapshot`, so an activation mid-turn never
  reprices the rest of the turn); None = `config.current()`. An alias missing from that snapshot costs 0,
  estimated, with one WARNING per alias (`price` never raises).
- **Line ids** (`noema.ops.cost.lines`): `decision_line_id`, `cache_savings_line_id`, `attempt_line_id`,
  `labeling_line_id` = `sha256(x)[:26]` per the spec-11 §2 table; `usage_line_record(line, at)` builds the
  outbox record (cost as a 6-decimal string, `at` RFC 3339 Z). Other domains compute the same ids themselves.
- **Usage report** (IMPL-040): `parse_query` (group_by default `line`, `to` = today UTC, `from` = `to` - 29 days,
  `from > to` or > 366 days -> 400 `schema_violation` param `from`), `UsageReporter(db).page/report` under
  `uow(principal)` (developer: project; tenant_admin/auditor: tenant, RLS), `render_report` writes exact decimal
  `cost_usd`. The route checks `authz.check(who, "usage.read")` (403 `permission_denied` otherwise).
- **Outbox** (IMPL-022): deque bound 100k; over the bound records go straight to the Redis stream, with Redis
  down the oldest is dropped (`aghoo_outbox_dropped_total`, ERROR). Writer: every 200 ms or 500 records,
  `outbox_apply`; rejects and Postgres-down batches -> `XADD outbox:<crc32(tenant)%8>` (bounded 2 s); after a
  Postgres failure batches skip Postgres for 1 s; both down -> memory. A batch that fails for another reason is
  re-applied one record at a time (poison isolation). Drainer: every 1 s `XAUTOCLAIM` (idle > 30 s) then
  `XREADGROUP` in group `drain`; delivery counts of claimed entries are read per entry (`XPENDING id id 1`);
  applied -> `XACK`+`XDEL`; failed -> pending; failed at delivery >= 10 -> `outbox_dead` + ack (ERROR log, plus an
  alert note for turn_finalize). Postgres down -> nothing acked or dead-lettered, and once an apply found it down
  each cycle probes it with an empty `tenant_uow('__platform__')` and skips claiming and reading until it answers:
  an outage adds at most one delivery to an entry. `session_pending` release after an applied or dead-lettered
  `turn_finalize`: compare-and-delete (Lua `GET == turn_id` then `DEL`, record id = turn id, chunks of 500), each
  attempt bounded by 2 s, retried after 1, 2, 4 ... 30 s. The writer runs it in its own task inside `run()` (never
  under the batch lock: a slow or blackholed Redis never slows Postgres writes); the drainer runs it inline;
  `flush` makes one last attempt. `WriteBehindOutbox.pending_releases` exposes the outstanding pairs.
- **Jobs** (spec-11 §4): `enqueue` = `JobRepo.insert` under `tenant_uow(tenant)` (pg_notify at commit; live
  duplicate by `dedupe_key` returns its id; `run_after` None = database now) and wakes local workers. `PgWorker`
  claims up to its free slots (SKIP LOCKED, lock 60 s) on LISTEN `jobs`, local enqueue, a freed slot and every
  5 s; extends every 20 s (refused -> the handler is cancelled and its result dropped); return -> done,
  `PermanentJobError` -> dead, other errors -> backoff 30 s x 2^(attempt-1), dead at `max_attempts`. The log
  follows the state `SystemCalls.finish_job` returns (request 1): `dead` -> ERROR "job dead" (so `media.process`
  alerts at 2 attempts and a 7-attempt job does not at 5), `queued` -> WARNING "retried with backoff", None (the
  worker no longer held the job) -> WARNING "finish ignored, the lease was lost". Stored
  `last_error` = `Type: message`, redacted, <= 200 chars. `drain(grace_s)` stops claiming; cancelling `run()`
  gives 10 s. Scheduler: every 60 s `ensure_periodic('retention.sweep', 3600)`, `('labeling.drift', 604800)`.
  Reaper: every 30 s `reap_stuck(120)`. Session and idempotency leases, stuck attempts and abandoned turns need
  no reaper here (spec-11 §6: free-on-expiry, take-over at `begin`, generation's watchdog, the orchestrator).
- **Notify bridge**: LISTEN `noema_events`; `{channel, payload}` -> PUBLISH the payload to
  `RedisKeys.config_changed() | calibrators_changed() | keys_revoked()`; unknown/malformed dropped; Redis down
  dropped (pods poll). The listener reconnects with 1..30 s backoff.
- **Idempotency** (IMPL-077/085): `begin` inserts or classifies (expired -> take over; other fingerprint ->
  `Conflict`; completed -> `Replay`, S3 body read, read failure -> 500 retryable with the record kept;
  not_stored -> take over; live in_flight -> `InFlight(resource_id)`; lapsed -> take over; conditional UPDATE,
  one winner, losers re-read; 5 rounds then 500 retryable). Tokens are `OwnedToken(IdemToken)` with tenant,
  project and owner, so an old owner's token never equals the taker's; pass only tokens this store issued.
  `heartbeat` renews 30 s. `complete(None)` -> not_stored; a body > 1 MiB is put to S3
  `<tenant>/idempotency/<record_id>` (SSE-KMS tenant key in staging/prod, AES256 in dev/test; one retry within
  2 s, then not_stored + `aghoo_idempotency_store_errors_total`) only while this owner still holds the record,
  then the row; then `PUBLISH idem_done {tenant_id, record_id}`. A take-over deletes the previous object
  (awaited before `begin` returns, never deferred: the new owner writes the same key). `await_result`:
  `idem_done` pub/sub (tenant-checked) + 250 ms poll; 504 `deadline_exceeded`; 500 when the original turns
  not_stored, its lease lapses or another owner takes it over. Postgres down -> 500 at `begin`. Time limits
  (virtual-clock driven, `deadline_scope`): SUBSCRIBE min(0.5 s, the deadline) (then poll only; the pubsub is
  closed at once, close bounded 0.5 s), PUBLISH 0.5 s, replay GetObject 2 s (then 500 retryable), stale
  DeleteObject 1 s (then left for the overwrite or the sweeper), S3 put 2 s. `complete` worst case: renew and row
  update (2 s pool timeout each) + put 2 s + publish 0.5 s, about 6.5 s, all shielded.
- **Retention** (spec-03 §5, spec-11 §6): pages `SystemCalls.retention_page(1000, after=<the previous page's last
  candidate>)` (keyset on `(expires_at, kind, id, tenant_id)`, request 5) until a page is empty, so a failing row
  stays behind the cursor: it never starves newer rows and is tried at most once per run. session -> turns, decisions,
  files (`FileService.purge`), then the row; turn, decision (label excerpts nulled by the database), file
  (`FileService.purge`); idempotency_record -> one unit of work per record: the row iff it is still expired
  (`delete_expired`), then its S3 object before the commit (an S3 failure rolls the row back; a record a new
  request took over since the page read keeps its row and its new owner's body, Wave E). Failures are counted and left for
  the next run (a run never stops early); the first 10,000 failed keys of a run are remembered, and rows failing in
  two consecutive runs -> ERROR (alert; the stable order makes a persistent cause fail the same oldest rows). One `audit_event` per tenant via the outbox (`action retention.sweep`,
  details = counts and `failed`). `RetentionSweeper(db, files, bodies, outbox, ids, clock, *, page=1000,
  max_remembered=10000)`, built by `make_handlers(services)`.
- **Health**: `/healthz` 200; `/readyz` 200 iff not draining and `config.current()` works, else 503. Neither has
  a body (request 2, decided in C2a: the probe is unauthenticated, so it discloses no dependency states; spec-11
  §7 and the contract summary say so). No dependency probe.
- **Prices tooling**: `python -m noema.ops.prices placeholder --config <yaml> [--region R] [--effective-from D]`
  prints a schema-valid TBD catalog for every price key and tenant region (all costs `cost_estimated` until
  real prices are filled in and imported with `noema-admin prices import`). Never used at runtime.

## Config keys and settings read
`models.<alias>.price_ref` / `.id`, `platform.cost.prices_ref` (through `ConfigSnapshot.prices`),
`platform.idempotency.ttl_s`; the callers pass the tenant's `home_region` as `region` (never the serving
region). Settings: `pod_id`
(idempotency owner, drainer consumer, worker id), `s3_bucket`, `env` (SSE arguments), the noema_app DSN.

## Metrics, spans, logs
Metrics: `aghoo_cost_usd_total{tenant,line}`, `aghoo_outbox_backlog{sink=memory|redis}`,
`aghoo_outbox_dropped_total`, `aghoo_idempotency_store_errors_total`, `aghoo_idempotency_attach_total{result}`
(emitted by the store, not the api middleware: `in_flight` once per duplicate `begin` attaches to a running
original; then per `await_result` its outcome `completed`, `deadline` or `lost`; so `in_flight` counts attaches
and the others split the JSON waits). No spans (none are declared for these paths). Loggers: `noema.ops.cost`
(WARNING unknown alias), `noema.ops.outbox`, `noema.ops.jobs` (ERROR "job dead"), `noema.ops.listen`,
`noema.ops.notify_bridge`, `noema.ops.idempotency`, `noema.ops.retention` (ERROR "failed in two consecutive runs",
"retention sweep stopped"), `noema.ops.reapers`, `noema.ops.health`.

## Error codes raised
`schema_violation` (usage params; bad cursor/limit from the repo), `permission_denied` (via the Authorizer),
`internal_error` (Postgres down at `begin`/`await_result`, unreadable stored body, lapsed original, take-over
contention, a replay GetObject over 2 s), `deadline_exceeded` (`await_result`). `CostMeter.price` never raises
for an unknown alias (priced TBD). The api
middleware turns `Conflict` into 409 `idempotency_key_reused` and adds `x-aghoo-idempotent-replay`. A replay
sends the retry's own `x-request-id` (the stored one is dropped), except on a stored error envelope, whose
`error.request_id` names the original request (Wave E). A keyed `stream: true` createTurn is marked dispatched as
soon as its turn started (the turn outlives the request), so its record stores `{sse, turn_id}` and a retry after a
dropped connection attaches to the event log instead of running a second turn (Wave E). The hook hands the store
the fingerprint bound to the caller's principal (`principal_fingerprint`), so another principal of the project
reusing a key with the same body is a `Conflict` (409), never a replay or an attach of someone else's response (keys
stay project-scoped, IMPL-077); and a JSON response the route `withhold()`s (`store: false`, or a data class whose
`store_content` is not `full`) is completed as not_stored: a retry runs again, and the record never keeps content
the turn or decision itself may not keep (Wave E).

## Tests
`make test MOD=ops_ledger` (unit + integration, compose Postgres/Valkey/moto; xdist-safe), `make test-conformance`.
- T-COST-1 `tests/unit/ops_ledger/test_cost.py`; T-COST-2 (ledger side) `test_lines.py` +
  `tests/integration/ops_ledger/test_usage_ledger.py::test_recorded_lines_land_once_and_sum_exactly`;
  estimator vectors `test_estimator.py`.
- T-USAGE-1 `tests/integration/ops_ledger/test_usage_ledger.py` (every group_by x limits 1/2/50 vs a reference,
  developer vs tenant_admin/auditor) + `tests/unit/ops_ledger/test_routes.py` (real middleware, validator on).
- T-OBX-1/2 `tests/integration/ops_ledger/test_outbox.py` (T-OBX-2: the next turn's acquire is `Busy` until the
  drain, then gets the next seq), `tests/unit/ops_ledger/test_outbox_writer.py` (background release, backoff,
  compare-and-delete, a hung Redis never slows `write_once`, a cancelled write keeps its batch); fault cases on
  Valkey `test_outbox_faults.py`: dead-lettered finalize releases `session_pending`, a late release keeps a later
  turn's key (real Lua), two drainers on the same entries apply each once, an outage adds at most one delivery,
  exact claimed delivery counts, and through toxiproxy (`valkey_proxy` fixture; skipped without `make up`) a
  blackholed Redis never slows the writer and a 40 ms Redis still drains exactly once.
- T-IDEM-1 `test_idempotency.py`; T-IDEM-2 `test_idempotency_bodies.py` (moto S3); `test_idempotency_faults.py`
  (virtual clock): the `idem_done` message alone wakes a waiter (hour-long poll), a blackholed Redis bounds
  `await_result` (SUBSCRIBE gives up at 0.5 s) and `complete` (PUBLISH 0.5 s), a hung S3 bounds the replay read
  (2 s, 500) and the stale delete (1 s, New), the attach metric.
- T-JOB-1/2 and the reaper `test_jobs.py` (T-JOB-2 with three pools; LISTEN wake proven on a virtual clock whose
  poll never fires; the dead ERROR at `max_attempts=2`, none for a 7-attempt job at attempt 5),
  `tests/unit/ops_ledger/test_worker.py` (also: the dead ERROR at a job's own `max_attempts` from the returned
  state, an ignored finish after the lease moved); T-RET-1 `test_retention.py` (plus: more failing rows than a
  page never starve newer ones, a row surviving its delete is tried once per run, failures past `max_remembered`
  never stop a run, the repeated-failure ERROR, the audit record applied through `ops.outbox_apply`).
- Pinned-snapshot pricing: `tests/unit/ops_ledger/test_cost.py` and the suite case
  `test_a_pinned_snapshot_prices_with_its_own_catalog[ledger]`; readyz without a body: `test_routes.py`.
- Conformance registrations: `cost_meter[ledger]`, `token_estimator[heuristic]`
  (`tests/unit/ops_ledger/conformance_impls.py`); `outbox[write_behind]`, `job_queue[postgres]`,
  `idempotency[postgres]` (`tests/integration/ops_ledger/conformance_impls.py`, a dedicated per-process database
  and `LedgerClock`, which ages Postgres with virtual time and never autojumps while real I/O is in flight). The
  dedicated database comes from `tests.fixtures.db.create_scratch_database`, which creates and comments it under
  the fixture's advisory lock (request 7, fixed in the shared fixture in C2a), and is re-created if it vanished.

## Known limits
- Deploy: the worker needs schema revision 0002 (`make db-migrate`): against a 0001 database `jobs.finish` returns
  a boolean (the finish itself is applied: each system call autocommits), `PgSystemCalls.finish_job` raises, and
  the worker logs "finish failed" for every job and never the "job dead" ERROR.
- Retention: a delete that reports success but removes nothing (none is known: every delete runs under the row's
  own tenant) is counted as deleted and found again by the next run, once per run; it raises no alert (the
  keyset cursor makes a loop impossible, so the C1 "seen again on the next page" check is gone).
- Callers that pass no `snapshot` to `CostMeter.price` still price with the current config (request 4 asks
  decision, generation and labeling to pass `ctx.snapshot`).
- No AWS Price List importer: the placeholder generator only; real prices are filled in by an operator.
- `complete` is bounded by about 6.5 s (see Idempotency); Postgres statements themselves have no command timeout
  (pool acquire 2 s), so a blackholed Postgres still blocks until TCP keepalive.
- Pod-vs-Postgres clock skew at a lease boundary can make `begin` answer a retryable 500 inside the skew window.
- Each waiting duplicate (`await_result`) holds one Redis pub/sub connection while it waits (a process-wide
  `idem_done` subscriber that dispatches to waiters would remove that; not done in Phase 1).
- The drainer's other Redis calls (XAUTOCLAIM, XREADGROUP, XACK) use the client's socket timeout; a slow Redis
  slows draining (no data loss: entries stay in the stream).
- A silently dead LISTEN TCP connection is noticed only by keepalives; workers still poll every 5 s and pods
  poll config/calibrators, so it only delays.
- T-IDEM-2's "no model call, no new usage line" is shown at the store (a replay never touches the outbox); the
  `/v1/decisions` end-to-end check and the recorder side of T-COST-2 belong to decision/orchestrator and C2.
