# Wave E edge fixer (E4:edge): verified defects in api, identity, ops, db, contracts, composition

Scope: `src/noema/{api,identity,ops,contracts,config,db}/**`, `src/noema/composition.py`, `src/noema/entrypoints/**`,
`web/server/**` and their tests, on the working tree of 2026-10-11 (tag `wave-c2b` plus the E1 sprint and the
concurrent E4 fixers' edits). Bedrock-only: no model API was called (the e2e stack runs on the wire mock); no AWS
resource was touched. Stack: `E2E_PROJECT=noema-e2e-edge E2E_PORT_BASE=28700 IMAGE_TAG=edgefix` (own images
`noema-{api,worker,media}:edgefix`), torn down at the end.

"Before" evidence: every new regression test was run against a scratch copy of the tree with my source files
restored to their pre-fix state (`/tmp/edge/before`, `PYTHONPATH` first; HEAD versions of the files that were
clean before me, the sprint versions of `api/overload.py`, `api/routes/turns.py` and `composition.py` reconstructed).
Result there (`pytest $(cat /tmp/edge/new_tests.txt)`, 36 tests): 34 failed, 2 passed, and the 2 that passed are
the intended controls (an `internal`-class keyed turn and decision are still replayed); `test_ingress_hardening.py`
runs there in a variant without the principal-bound fingerprint assertion (that helper does not exist before).
After the fixes: 36 passed.

## 1. Defects and their fixes

| # | Gate (06 §11 / spec-10) | Fix (files) | Regression test (fails before) | Status |
|---|---|---|---|---|
| 1 | C1, F10, T-ADM-2 | A writing RESERVE carries a random marker key; on any failure `RedisStore` sends one bounded REFUND with it, which refunds the buckets only if the marker says the reserve wrote, removes the slot members, and voids a late reserve (status 3). Failed undos keep their slot releases (pending path). Only a call that started while degraded (a probe) ends the outage, so an in-flight success no longer flips it back. The 50 ms bound is kept (spec-11 §1.2 says 50 ms): with the undo a spurious timeout only costs 1 s on the local share. (`ops/admission/{lua,stores}.py`) | `tests/integration/ops_admission/test_failover.py::test_a_reserve_whose_reply_came_too_late_is_compensated_on_redis` (before: ZCARD 1 phantom member); `tests/unit/ops_admission/test_failover_store.py::test_a_success_that_started_before_the_cut_does_not_end_the_degraded_state`; `tests/integration/ops_admission/test_lua_gcra.py::test_an_undo_with_the_marker_is_exactly_once_and_voids_a_late_reserve` | fixed |
| 2 | T-IDEM-1/2, G12 (IMPL-077) | A keyed `stream: true` createTurn is marked dispatched as soon as `service.start()` returned (the turn outlives the request): the record stores `{sse, turn_id}` and the retry attaches. (`api/routes/turns.py`) | `tests/unit/api/test_turn_route_hardening.py::test_a_keyed_stream_dropped_before_its_first_model_call_is_attached_on_retry` (before: "one Idempotency-Key ran 2 turns") | fixed |
| 3 | C2 (lag p99 < 50 ms), C5 | JSON bodies over 1 MiB (`OFFLOAD_BYTES`) are parsed, schema-checked and (keyed) fingerprinted in a worker thread (`anyio.to_thread`); the canonical fingerprint is unchanged. (`api/middleware/ingress.py`, `idempotency.py`) | `tests/unit/api/test_ingress_hardening.py::test_a_large_body_is_checked_and_fingerprinted_off_the_event_loop` (before: the check ran on the loop thread) | fixed |
| 4 | C2 memory per stream, C4 | The raw bytes are dropped after the gate: `RequestContext.body` is gone, `replay_receive` gets `b""` and releases what it delivers, the local is `del`eted before the route runs. (`api/middleware/{ingress,body,context}.py`, `api/deps.py`) | `test_ingress_hardening.py::test_the_raw_body_is_not_kept_after_the_gate` | fixed |
| 5 = 21 | (08 §7 correlation) | createTurn passes `deps.trace_id(ctx)`: the joined W3C trace id, else sha256(request id)[:32] (the formula of `decision.service.trace_id_of`; routes may not import another domain, rule 5). (`api/deps.py`, `api/routes/turns.py`) | `test_turn_route_hardening.py::test_a_turn_without_traceparent_gets_a_per_request_trace_id` (before: `'0'*32`) | fixed |
| 6 = 20 | T-RET-1 (IMPL-077/085) | The sweeper deletes an idempotency record in its own unit of work with `PgIdempotencyRepo.delete_expired` (`... AND expires_at < now() RETURNING id`) and deletes its S3 object before the commit only when the row went; an S3 failure rolls the row back. (`db/repos/idempotency.py`, `ops/retention/sweeper.py`) | `tests/integration/ops_ledger/test_retention.py::test_a_record_taken_over_after_the_page_read_keeps_its_row_and_object` (before: the live record was deleted) | fixed |
| 7 = 23 | SE15 (SEC-EDGE-01), S2 | Only what the headers declare (Content-Length over the cap: 413; Content-Type: 415) is decided before authentication; the body is read after step 4, so a keyless client makes the process buffer nothing. Precedence change: an undeclared-length body over the cap without a key is 401 (with a key still 413). (`api/middleware/ingress.py`) | `tests/unit/api/test_body_cap.py::test_an_unauthenticated_streamed_body_is_never_buffered` (before: 10 MiB pulled), `::test_an_undeclared_size_over_the_cap_is_413_after_authentication`; `tests/integration/api/test_uvicorn_server.py::test_a_chunked_body_is_cut_off_at_the_cap` now authenticates | fixed |
| 8 | SE15 (SEC-CFG-01), IMPL-044 | Migration `0003_operator_proof`: `admin._operator` now requires the connection's session setting `noema.operator_key` (`{v, p, k, s}`) to prove the operator's key (HMAC-SHA256 recomputed by the new internal `admin._hmac_sha256` against `platform.operator.key_hash`). The CLI sets it after its own check (`PgAdminCalls.prove_operator`) and again after a pepper re-hash. No table, column or granted function changes (IMPL-010, spec-03 §4). (`db/alembic/sql/0003_operator_proof/*`, `db/alembic/versions/r0003_operator_proof.py`, `db/system.py`, `identity/cli/context.py`) | `tests/integration/db/test_admin_config.py::test_a_connection_acts_only_as_the_operator_whose_key_it_proved` (before: "DID NOT RAISE": an id alone approved) | fixed |
| 9 | SE3, P1-C4 | Fixed by the engines fixer in `decision/service.py` (`data_class_of` returns `restricted` when the project read fails); verified by `tests/unit/decision/test_routes.py::test_the_data_class_fails_closed_when_the_project_cannot_be_read` in my unit run. No change in my paths. | (theirs) | fixed (engines) |
| 10 | data-class storage (03 L133, L420; spec-09) | The hook gets `withhold()`: a JSON response whose content the request may not keep (`store: false`, or a data class whose `store_content` is not `full`) is completed as not_stored (a retry runs again); SSE records hold only the turn id and are unaffected. createTurn, createDecision and the facade call it. (`api/middleware/idempotency.py`, `api/deps.py`, routes) | `test_turn_route_hardening.py::test_a_keyed_json_turn_whose_content_may_not_be_kept_is_not_stored[*]`; `tests/unit/api/test_decision_route_hardening.py::test_a_keyed_decision_is_replayed_only_when_its_content_may_be_kept[*]` | fixed (session.delete purge: remaining) |
| 11 | C5 (fairness) | `TurnSlots` is per tenant: share = cap // tenants holding slots (plus the caller); a tenant below its share is admitted up to `burst` = cap // 10 above the cap, one at or above it only below the cap. One tenant alone still gets the whole cap (L1 behavior unchanged), but a newcomer is admitted at once and freed slots go to it; a process holds at most cap + cap // 10. (`api/overload.py`, `api/routes/capacity.py`) | `tests/unit/api/test_overload.py::test_one_tenant_at_the_cap_does_not_keep_another_out` | fixed |
| 12 | F11 (hung), 06 §7 | The DB part: see 16 (every app-pool statement bounded; a heavy usage report now fails at 10 s with the retryable 500). Waiters (one pub/sub plus a 250 ms poll per JSON duplicate) and an ingress request-rate bucket for non-model requests: not changed (spec-11 §1.2 rates model calls only). | (16's tests) | partial |
| 13 | SE15 (edge caps) | Session metadata is at most 16 pairs, keys <= 64 and values <= 512 characters on create and update (400 `schema_violation`, `param` `metadata` / `metadata.<key>`); the vendored contract cannot change in Wave E. (`identity/sessions.py`) | `tests/unit/identity/test_sessions_routes.py::test_metadata_is_bounded_on_create_and_update` (before: 201) | fixed |
| 14 | SE1 (zero leaks), spec-09 §3 | The fingerprint the store compares is bound to the principal (`principal_fingerprint`): another principal reusing a key with the same body is 409 `idempotency_key_reused`, never a replay or an attach (keys stay project-scoped, IMPL-077; squatting of a predictable key stays possible, see §4). (`api/middleware/idempotency.py`) | `test_decision_route_hardening.py::test_another_principals_key_is_a_conflict_never_a_replay` (before: 200 with the other principal's result) | fixed |
| 15 | spec-11 §7, Q1 | The snapshot is pinned after routing and inside the error handling: `/healthz` 200 and `/readyz` 503 before the first config load; an authenticated operation gets the typed, contract-valid retryable 500 with `x-request-id`. (`api/middleware/{ingress,context,validation}.py`) | `tests/unit/api/test_no_config.py::test_probes_and_typed_errors_before_the_first_config_load` (before: Starlette's plain-text 500 raised through) | fixed |
| 16 | F11 (hung variant), 06 §7 | `db.engine.app_pool_settings` (used by composition): driver command timeout 10 s, server `statement_timeout` 10 s and `lock_timeout` 5 s; `Engine.acquire` maps asyncpg's own `TimeoutError` (innermost frame in asyncpg), `QueryCanceledError` and `LockNotAvailableError` to the retryable `db_unavailable()`; a caller's own `TimeoutError` still propagates (existing test). (`db/engine.py`, `composition.py`) | `tests/integration/db/test_uow.py::test_a_hung_postgres_fails_fast_as_internal_error` (blackhole proxy; before: no bound, the reviewer measured > 12 s pending); `tests/unit/composition/test_background.py::test_the_app_pool_bounds_every_statement` | fixed |
| 17 | Q1, spec-08 §4 | An error carrying `x-ratelimit-*` headers (`AdmissionRejected.headers`) sends them with its envelope. (`api/middleware/ingress.py`) | `test_ingress_hardening.py::test_an_admission_429_carries_the_rate_limit_headers` (before: only `retry-after`) | fixed |
| 18 | Q1 (IMPL-033) | A replay drops the stored `x-request-id`, so the retry's own id is sent; a stored error envelope keeps its original (spec-08: the header equals `error.request_id`). (`api/middleware/idempotency.py`) | `tests/unit/api/test_idempotency_hook.py::test_a_replay_echoes_the_retrys_own_request_id` (before: `client-req-1`) | fixed |
| 19 | G12, overload | A keyed request found at the cap waits for step 7: a duplicate attaches or replays without a slot, a new keyed turn is shed then (its record is not stored). Unkeyed requests are still shed before any DB work. (`api/routes/{turns,capacity}.py`) | `test_overload.py::test_a_keyed_duplicate_at_the_cap_attaches_instead_of_being_shed` (before: 429) | fixed |
| 22 | F10, SEC-AUTH-02 | The process Redis client sends TCP keepalives (30 s idle, 3 x 10 s); the `keys_revoked` listener PINGs its quiet connection every 5 s and resubscribes after 15 s with no message or PONG; `PgListener` runs `SELECT 1` (2 s bound) every `CHECK_S` and reconnects on failure. (`composition.py`, `identity/revocation.py`, `ops/notify_bridge/listener.py`) | `tests/integration/identity/test_revocation.py::test_a_silently_dead_subscription_is_detected_and_resubscribed` and `tests/integration/ops_ledger/test_notify_bridge.py::test_a_silently_dead_listen_connection_is_replaced` (blackhole proxy); `tests/unit/identity/test_security_events.py::test_a_connection_that_never_answers_is_resubscribed`; `test_background.py::test_the_redis_client_sends_tcp_keepalives` | partial (turns/adapters/labeling listeners: keepalive only) |
| 25 | C1/C2 overload | createDecision and the facade take a slot of the same cap for their whole duration (counted in `aghoo_turns_shed_total`). (`api/routes/{decisions,openai_compat,capacity}.py`) | `test_overload.py::test_decisions_and_the_facade_are_under_the_cap` | fixed |
| 26 | SE1 (authfail bucket, ip_allowlist) | `client_ip` treats an IPv4-mapped peer or hop as its IPv4 address (as `authn` already did). (`api/middleware/client_ip.py`) | `tests/unit/api/test_client_ip.py::test_ipv4_mapped_peers_and_hops_are_their_ipv4_address` | fixed |
| 28 | SE12 (trace scanning), 08 §7 | `build()` passes `otlp_traces_endpoint(environ)` (`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, else `OTEL_EXPORTER_OTLP_ENDPOINT` + `/v1/traces`); unset, nothing changes. (`composition.py`) | `test_background.py::test_the_otlp_variables_wire_a_trace_exporter` | fixed |
| 30 | SE12, spec-09 §2 | The access line's message carries route, status, latency, request id and key id, plus the `tenant` and `event` keys the JSON formatter keeps (the formatter is fakes-telemetry's). (`api/middleware/ingress.py`) | `test_ingress_hardening.py::test_the_json_access_line_carries_the_request_fields` (before: `KeyError: 'tenant'`) | fixed |
| 31 | F11 (`store: false` unaffected), 06 §7 | `KeyCache` keeps an expired positive entry `STALE_S` = 600 s as the last good row; while Postgres fails a lookup it is used (not refreshed; Postgres is retried after 5 s); revocation still evicts it. (`identity/{keycache,authn}.py`) | `tests/unit/identity/test_authn.py::test_postgres_down_serves_the_last_good_key_row` | fixed |
| 24, 27, 29, 32 | F2/F10/SE3/SE12; L1/C2; SE13; K1 | Outside my paths or needing P0 data: see §4. | — | not fixed |

## 2. Checks run

| Gate | Command | Pass criterion | Measured | Result |
|---|---|---|---|---|
| X1 / make check (static) | `make lint typecheck typecheck-tests importlint forbidden-scan contracts-check` | all green | ruff 867 files clean; mypy --strict 322 src + 546 test files 0 issues; import-linter 27 kept 0 broken; forbidden-scan clean; `_vendor` identical to the canonical contracts (70 files) and generated models current | PASS |
| make check (tests) | `make test-unit test-conformance test-integration sdk-check NOEMA_AGENT_ID=qa-edgefix` | 0 failures | unit 3186 passed; conformance 757 passed, 8 skipped; integration 547 passed, 21 skipped (148 s); sdk-check: TS 86 passed / 6 skipped, Python 192 passed / 1 skipped (live) | PASS |
| make check (ownership) | `make ownership` | every changed path has an owner | 19 unowned paths, all created by the E1 QA contract track (`make/qa-{contract,fault,security}.mk`, `tests/qa/contract/**`); every path I touched maps to its role | FAIL (not in my paths: needs OWNERS.yaml rules from platform/orchestrator) |
| Regression tests | the 36 tests of §1 on `/tmp/edge/before` (pre-fix sources) and on the tree | each fails before (controls excepted) and passes after | before: 34 failed, 2 passed (the two controls); after: 36 passed (and in the totals above) | PASS |
| e2e (spec-10 Q1 on the stack; G01-G12, multipod, authz, usage, files, lifecycle, shutdown) | `make e2e E2E_PROJECT=noema-e2e-edge E2E_PORT_BASE=28700 IMAGE_TAG=edgefix` on a fresh stack (images rebuilt from the final tree), then again with `E2E_BUILD=0` | 52/52 both runs, every response contract-validated | run 1: 52 passed (154 s); run 2: 52 passed (153 s); 0 tracebacks in either api log; an earlier run on an intermediate tree also 52/52 | PASS |
| SE15 / SEC-CFG-01 live | on the e2e stack: `psql -U noema_cli ... SELECT admin.list_config_versions('<operator id>')` without a key proof | refused | `ERROR: operator_invalid: unknown, revoked or unproven operator`; the e2e multipod test (propose A, approve B through noema-admin) passes, so the CLI proof works end to end; the stack runs `0003_operator_proof` | PASS |
| SE15 / SEC-EDGE-01 live | `curl -H 'Transfer-Encoding: chunked' --data-binary @20MiB http://127.0.0.1:28701/v1/turns` (no key) | 401 before the body is read | `401 0.002s up=0` (0 body bytes taken), `/healthz` 200 | PASS |

## 3. Behavior changes that need an owner note (each documented in docs/modules)

1. Error precedence (7): spec-04 §1 orders step 3 (413/415) before step 4 (401). Kept for what the headers
   declare; a body of undeclared length is read only after authentication, so without a key it is 401, not 413.
   (`integration.md` §9.1, the ingress docstring.)
2. Operator proof (8): revision `0003_operator_proof` replaces the internal `admin._operator`; spec-03 §4's
   function list and IMPL-010's tables are unchanged. The proof (pepper and secret) reaches Postgres as a bind
   parameter on the CLI connection, so production databases must not log statements with parameters
   (`log_statement=all`). (`db.md` "Revision 0003", `identity.md`.)
3. Idempotency records (10, 14): IMPL-077 says "stored iff a model call was dispatched". Now also "and the
   request may keep the content" (`store: false` or a non-`full` data class: not_stored, so a retry re-executes
   and is billed again), and the stored fingerprint is bound to the principal (a cross-principal reuse is 409).
   Keys stay project-scoped, so a principal can still squat another's predictable key for its TTL; per-principal
   scope (`scope_hash`) would change IMPL-077/spec-11 §5. (`ops-ledger.md`.)
4. The per-process cap (11, 19, 25): a process may hold cap + cap // 10 when tenants below their share arrive;
   decisions count against the same cap; keyed requests at the cap pay authorization and one idempotency begin
   before being shed. (`integration.md` §5.1.)
5. Database bounds (16): 10 s per statement (driver and server), 5 s lock wait, for api, jobs and media-worker
   alike; a usage report or job statement above 10 s is now a retryable 500 instead of running on. (`db.md`.)
6. Session metadata bounds (13): 16 pairs / 64 / 512 characters, server-side only (the contract has no bounds;
   a request the schema accepts can now be 400). (`identity.md`.)
7. Stale key rows (31): with Postgres and Redis down at once, a key revoked just before stays usable for at most
   `STALE_S` = 10 min (with Redis up, `keys_revoked` still evicts at once). (`identity.md`.)
8. Admission (1): the 50 ms bound of spec-11 §1.2 is kept. Event-loop stalls above it still count as "Redis
   down" for 1 s (local share), but no longer leave a Redis debit behind.

## 4. Not fixed here (with the reason and the proposed fix)

- 24 (metrics of the second api worker invisible to the gate helpers): the fix is in `tools/bootstrap_dev.py`
  (record 9464..9464+WEB_CONCURRENCY-1 per replica), `tests/e2e/suite/kit.py`, `tests/fault/stackctl.py`,
  `tests/security/stack/{capture,test_cache_isolation}.py` (sum over every worker port; assert the endpoint count
  equals replicas x workers). None of these is in my paths (integrator tools, QA tracks). Until then counter-delta
  gates on 2-worker stacks (F2, F10, SEC-ISO-02, SE12 capture) see half the processes; run them with
  `NOEMA_STACK_API_WORKERS=1`.
- 27 (`core/clock.deadline_scope` starts a watcher task even on `SystemClock`, ~4% CPU per turn): `noema.core` is
  the platform's (frozen); the measured fast path (`anyio.CancelScope(deadline=...)` for `SystemClock`) is in
  `/tmp/noema-b/snap-diag-clock/src/noema/core/clock.py`.
- 29 (SE13: 2 CRITICAL + 55 HIGH OS CVEs in api/worker, 4 + 94 in media, no fixed versions): `deploy/docker/**`
  and `reports/security/waivers.md` are not mine. Needs either a base image without them or a waiver with reason
  and expiry per spec-09 §9.
- 32 (K1 estimator, chars/4 for every model): the formula is IMPL-029c / spec-11 §2 and K1 is "live-n (cost P0)"
  in spec-10. The live sample (n = 1-13 per model) suggests per-model `chars_per_token` (Claude about 3.1-3.2,
  DeepSeek about 3.4, Luna 4.0) plus a fixed per-request overhead (about 15 tokens for Opus/Sonnet, 25 for GLM);
  changing the admission estimates on that sample is a calibration decision for P0, not an edge fix. Until then
  the TPM reservations under-reserve Claude input on live (the output worst case usually covers it).
- 22, rest: the turn-cancel and session-released subscriptions (`turns/coordination.py`), `breaker_events`
  (adapters) and the calibrator listener (labeling) now get TCP keepalives from the shared client (a dead peer is
  noticed within about a minute) but no application-level PING like `keys_revoked`; a shared helper would have to
  live in `noema.core` (platform). Owners: engines, io.
- 10, rest: `session.delete` does not purge the session's idempotency records (full-content classes keep a JSON
  replay up to `platform.idempotency.ttl_s` after the deletion). Needs turn and decision ids of the session in
  the purge (new repo queries beyond the frozen protocols).
- 12, rest: each JSON duplicate still opens its own `idem_done` subscription and polls Postgres every 250 ms until
  its deadline; a shared per-process subscriber (as the io fixer did for watchFile) would bound it.
- From `.orchestration/requests/io-fixer.md` item 3 (not in my verified list): watchFile has no concurrency cap,
  and the process Redis client keeps redis-py's default pool size.

## 5. Existing tests changed with the behavior

- `tests/unit/api/test_body_cap.py`: the streamed over-cap case without a key is now 401 (`..._413_after_authentication`).
- `tests/unit/api/test_overload.py`: `TurnSlots.acquire(tenant, running)` / `release(tenant)`; `starting` is a dict.
- `tests/unit/api/test_idempotency_hook.py`, `test_ingress_hardening.py`: fingerprints compared through
  `principal_fingerprint`.
- `tests/integration/api/test_uvicorn_server.py::test_a_chunked_body_is_cut_off_at_the_cap`: sends a key.
- `tests/integration/db/test_admin_{config,keys_calibrators}.py`, `tests/integration/labeling/test_registry_postgres.py`:
  operators get real key hashes (`tests/integration/db/_admin.py`: `operator`, `login`, `Acting`); the re-hash test
  proves the key as stored after each change; `test_migrations.HEAD` is `0003_operator_proof`.
- `tests/unit/identity/test_cli_refusals.py`: the fake records the proofs (as stored, then after the re-hash).
- `tests/unit/identity/test_security_events.py`: `FlakyPubSub.ping`.

Teardown: `make e2e-down E2E_PROJECT=noema-e2e-edge E2E_PORT_BASE=28700` (containers, volumes and network removed),
the `noema-*:edgefix` images and the stack's state file deleted; the shared `noema-dev` services were left running.
