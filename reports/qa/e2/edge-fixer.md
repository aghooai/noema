# Wave E2 edge fixer: SSE close, resume visibility, cross-pod attach, NUL ids, idempotency, watchFile

Scope (binding): `src/noema/api/**`, `src/noema/ops/idempotency/**`, `src/noema/db/**` (no schema change made),
`src/noema/identity/**` (session delete), `src/noema/media/pipeline.py` (watchFile only) and their tests. Bedrock-only:
no model API was called, no live Bedrock call (every stack runs on the wire mock), no AWS resource touched, no git
write command. Own stacks, all torn down at the end (containers, volumes, state files, the `noema-*:e2edge` images):
e2e `noema-e2edge` (port base 27100), contract `noema-e2edge-contract` (27500), fault `noema-e2edge-fault` (27300);
images built from the working tree as `noema-{api,worker,media}:e2edge` (source sha256 of the touched files checked
equal in the image). Port bases above 32767 collide with the host's ephemeral range (a first attempt at 33700 failed
to bind 33774), hence the 27xxx bases.

"Before" evidence: every regression test was written and run before its fix; the failure is quoted per row. Where
the fix adds a new name the test imports, the before-run used a variant without that import (stated in the row).

## 1. Items

| Item (gate) | Fix (files) | Regression test: before -> after | Gate on my stack |
|---|---|---|---|
| QL-1 / edge #24 / fault B2 (C3) | `SseResponse.__call__` runs `on_close(outcome)` in a `finally`, before any further send (so on every path; "disconnected" when the writer raised). After `client_too_slow` the closing empty body is bounded by `FINAL_SEND_TIMEOUT_S` = 1 s (uvicorn holds that send until the client drained its backlog); past it the response returns unfinished and uvicorn closes the connection. "completed" still sends the closing body unbounded. (`api/sse_frame.py`) | `tests/unit/api/test_sse_frame.py::test_a_too_slow_client_is_closed_before_the_final_send_and_that_send_is_bounded`: before `TimeoutError` (the closing send never returned, `on_close` never ran) -> pass, order `on_close client_too_slow`, then the send; `::test_on_close_runs_when_the_writer_fails`: before `[] == ['disconnected']` -> pass | fault-scale C3 (`test_c3_slow_clients.py`): 4/4 slow turns stored **`failed`**, server end 47.4-48.8 s into the 90 s stream (was 4/4 `completed` at 90.6 s); 8/8 normal clients completed, TTFT p95 728.2 ms vs baseline 774.3 ms: PASS |
| Edge #22 (SE1) | resumeTurnEvents calls `TurnService.get(principal, turn_id)` (V(turn), 404 `not_found` outside it) before the Phase-1 501. (`api/routes/turns.py`) | `tests/unit/api/test_turn_route_hardening.py::test_resume_turn_events_checks_turn_visibility_before_the_phase_1_501`: before `(501, 'not_implemented') == (404, 'not_found')` for a peer developer -> pass (own 501, peer 404, unknown 404) | `tests/security/stack/test_authz_matrix.py` on `noema-e2edge`: **356 cells, 356 equal to the oracle, 0 leaks** (Wave E final: 348 + 8 `resumeTurnEvents` mismatches); 5/5 tests: PASS |
| Edge #25 (F10, IMPL-021) | A streaming duplicate whose original runs on another pod with the mirror unavailable no longer answers 500 at once: it waits for the original (`await_result`, bounded by the request deadline), then is its replay: 410 `resume_window_expired` with `details.turn_id` while the mirror stays unavailable (500 retryable if the original ended unstored). (`api/middleware/idempotency.py`) | `tests/unit/api/test_idempotency_hook.py::test_an_unattachable_cross_pod_duplicate_waits_for_the_original_then_410[True,False]`: before both `assert [<Response [500]>] == []` (answered at once) -> pass | `test_f10_redis_down.py` on `noema-e2edge-fault`: `[down]` cross-pod attach **410** `resume_window_expired` after 2,543 ms, `[hung]` **410** after 3,274 ms (both after the original ended); 125/125 turns completed during each outage, cancels 202/500 as specified: PASS |
| Finding 17 (Q1, ruling R7) | Step 5 (`api/middleware/gate.py` `check_params`): a path `*_id` holding a control character (U+0000-U+001F, U+007F-U+009F) is that operation's 404 (`session_not_found` for session ids, else `not_found`), a query string parameter holding one is 400 `schema_violation` (`param` its name); neither reaches the route. createSession/updateSession metadata holding U+0000 is 400 `schema_violation` (`param` `metadata` or `metadata.<key>`, a gate semantic rule). Body ids: the repositories' `get` of files, sessions, turns and decisions answer None for an id holding U+0000 without a statement (`db/repos/_base.unstorable`), so each caller answers its own 404; a pagination cursor holding U+0000 is malformed (400 `param` `after`). | `tests/unit/api/test_gate_step.py::test_control_characters_in_ids_and_query_strings_never_reach_the_route[5 cases]`: before all 5 reached the route (`ContractViolation: ... status 204`) -> pass; `::test_session_metadata_holding_nul_is_400[4 cases]`: before 501 (passed the gate; on the stack: 500) -> pass; `tests/integration/db_repos/test_nul_ids.py` (2): before `CharacterNotInRepertoireError: invalid byte sequence for encoding "UTF8": 0x00` -> pass | `tests/qa/contract/test_input_robustness.py` on `noema-e2edge-contract`: **18/18 NUL id cases 400/404** (Wave E final: 2/18), the 3 stored-text cases pass, 67/67; schemathesis stateless 25 operations 0 failures (uploadFile deselected, see §4); server-side ContractValidator 0 violations, 0 tracebacks in the api logs: PASS |
| Edge #2 rest (F11 hung) | No change: already in the tree since Wave E (edge fix 16). `db.engine.app_pool_settings` is the only app pool (`composition._DbPool`): driver command timeout 10 s, `statement_timeout` 10 s, `lock_timeout` 5 s, mapped to the retryable `internal_error`. | guard (no new test): `tests/integration/db/test_uow.py` 18 passed, incl. `test_a_hung_postgres_fails_fast_as_internal_error` (blackhole proxy) | in make check: PASS |
| Edge #8 rest (06 §5, IMPL-032) | createDecision runs `prepare_native` (its `check_files` read Postgres) under `deadline_scope(ctx.deadline)`: 504 `deadline_exceeded` when the deadline passes first (before any dispatch: no row, no usage). `prepare_native`'s signature is decision's, so the route bounds the call. (`api/routes/decisions.py` `_prepare`) | `tests/unit/api/test_decision_route_hardening.py::test_the_file_checks_run_under_the_request_deadline` (a file lookup that never answers, deadline 2 s, virtual clock): before `TimeoutError` (no answer within 10 s real time) -> 504 `deadline_exceeded` | unit (contract-validated app): PASS |
| Defect 10 rest (session data, spec-03 §5) | `session.delete` expires the keyed-request records of the session's turns and decisions before their rows go (and again for a turn finalized meanwhile): `expires_at = now()` (never replayed again; the hourly retention sweeper deletes the row and any large S3 body) and the inline body cleared at once. (`db/repos/idempotency.py` `PgIdempotencyRepo.expire_for_session`, `identity/jobs.py`) | `tests/integration/identity/test_sessions_pg.py::test_session_delete_expires_the_idempotency_records_of_its_turns`: before `('idem_mine', False, b'{"text": "SECRET"}') != ('idem_mine', True, None)` -> pass (another session's record keeps its replay) | e2e 52/52 incl. `test_lifecycle.py` (deleteSession through the jobs process on Postgres): PASS |
| Defect 12 rest (06 §5) | `DoneSubscriber`: ONE `idem_done` pub/sub connection per store (process) for every waiting JSON duplicate; channels subscribed while they have a waiter, closed when none is left; a waiting waiter reads it for all (one reader at a time), tenant-checked dispatch; SUBSCRIBE still bounded by min(0.5 s, deadline), a failed subscription resets the connection and its waiters keep the 250 ms poll. (`ops/idempotency/waiting.py`, `store.py`) | `tests/integration/ops_ledger/test_idempotency_faults.py::test_the_waiters_of_one_process_share_one_idem_done_connection` (8 duplicates of 8 originals, hour-long poll): before `assert 8 == 1` pub/sub connections -> 1, each waiter woken by its own message (virtual clock unchanged) | the existing message-only wake-up and blackholed-Redis bound tests: PASS |
| Defect 22 rest (F10 hung) | Application-level heartbeat on the two shared subscriptions in my paths: while read, a quiet connection is PINGed every 5 s and given up after 15 s without any reply (a hung proxy keeps TCP alive, so the Wave E keepalives never fire). `idem_done`: reset, waiters poll. File events: no longer `live`, every watcher is woken to re-read and falls back to the 2 s poll, the connection is dropped so the next reader reconnects (redis-py re-subscribes). The four listeners named in the item are outside my paths (see §4). | `tests/unit/ops_ledger/test_idem_done_subscriber.py::test_a_quiet_connection_is_pinged_and_reset_only_when_nothing_answers[False,True]` (new component: before `ImportError`); `tests/unit/media_pipeline/test_watch_polling.py::...[False-24]` (silent connection: dead at 15 s, 24 row reads in 61 s, last read not live) | unit (virtual clock): PASS; partial for the item |
| CR item 3a (watchFile cap) | `WatchSlots` (`api/overload.py`, `app.state.watch_slots` in `create_app`): at most 32 open watchFile streams per (tenant, project) and process; above it 429 `rpm_exceeded` (the only 429 code watchFile declares) with `retry-after: 5`, after V(file) and before the stream starts; the place is released in the stream's `on_close`. (`api/routes/files.py`) | `tests/unit/api/test_watch_cap.py::test_watch_streams_are_capped_per_project_and_give_their_place_back` (cap 2): before (variant without the slots) `[200, 200, 200] == [200, 200, 429]` -> pass; a closed stream gives its place back; contract validator 0 violations | unit: PASS |
| CR item 3c (re-read storm) | A watcher re-reads its row on events and every `WATCH_POLL_S` = 30 s while its event channel is `live`, every `POLL_S` = 2 s otherwise (fallback, Redis down, `LocalFileEvents`). (`media/pipeline.py`: `FileEventWaiter.poll_s`, `RedisFileEvents` liveness and heartbeat, `MediaFileService.watch`; `from_services` passes the clock) | `tests/unit/media_pipeline/test_watch_polling.py::test_a_live_event_channel_spares_the_two_second_rereads[True-3]`: before (variant without `clock=`) **31 row reads per watcher in 61 s** -> 3 | unit: PASS; `tests/integration/media_pipeline/test_pg_s3_redis.py` 8 passed |
| CR item 3b (Redis pool size) | not fixed: the client is built in `composition._redis_client` (not in my paths) | - | see §4 |
| R4 (pool and datastore metrics) | not fixed: needs metric declarations in the frozen `telemetry/registry.py` and wiring in `composition.py` (neither in my paths) | - | see §4 |

## 2. Checks

| Gate | Command | Pass criterion | Measured | Result |
|---|---|---|---|---|
| make check (full, final tree) | `make check NOEMA_AGENT_ID=e2edge` | every step green | lint (894 files), mypy --strict 323 src + 572 test files, import-linter 27 kept, forbidden-scan, ownership, contracts-check (70 files identical, generated current), unit 3,284 passed, conformance 757 passed / 8 skipped, integration 561 passed / 21 skipped, sdk-check TS 86 / 6 skipped, Python 192 / 1 skipped; "make check: all green" | PASS |
| Regression tests (determinism) | the 13 new or changed test files, 3 consecutive runs | identical | 131 passed x3 | PASS |
| e2e (spec-10 §1; Q1 per response) | `make e2e E2E_PROJECT=noema-e2edge E2E_PORT_BASE=27100 E2E_BUILD=0` with `NOEMA_{API,WORKER,MEDIA}_IMAGE=noema-*:e2edge` (2 api workers) | 52/52, every response contract-validated | 52 passed (161 s) | PASS |
| SE1 authz matrix | `NOEMA_E2E_STATE=~/.noema/stacks/noema-e2edge.json NOEMA_E2E_PROJECT=noema-e2edge pytest -m e2e tests/security/stack/test_authz_matrix.py` | every cell = oracle, 0 leaks | 356 / 356, 0 leaks; the 18 resumeTurnEvents cells: 501 where the turn is visible (own; tenant_admin and auditor within their tenant), 404 everywhere else | PASS |
| Q1 NUL reproducers + schemathesis | `make qa-contract QA_CONTRACT_PROJECT=noema-e2edge-contract QA_CONTRACT_PORT_BASE=27500 QA_CONTRACT_BUILD=0` (images as above), then `pytest -m e2e tests/qa/contract/test_input_robustness.py` and `tests/qa/contract/test_schemathesis_stack.py --deselect '...[POST /v1/files]'` on that stack | NUL ids 400/404, no 5xx, 0 validator violations | input robustness 67/67 (18/18 NUL ids); stateless schemathesis 25 operations 0 failures; validator violations 0; tracebacks 0. The full `qa-contract` run: 129 passed, 2 failed, both from the contracts fixer's new uploadFile encoding (§4) | PASS for my items; qa-contract FAIL outside my paths |
| F10 cross-pod attach, C3 slow clients | `NOEMA_STACK_API_WORKERS=1 make qa-fault FAULT_PROJECT=noema-e2edge-fault FAULT_PORT_BASE=27300 FAULT_IMAGE_TAG=e2edge FAULT_BUILD=0 FAULT_RESULTS=/tmp/e2edge/fault-results.json FAULT_TESTS="tests/fault/test_f10_redis_down.py tests/fault/test_c3_slow_clients.py"` (1 worker: the fault metric helpers still read one port, defect 24) | F10: attach waits then 410; C3: slow turns cancelled (`failed`), others unaffected | 3/3 passed (411 s); numbers in §1 | PASS |
| C3 at load scale (`run.sh e5-c3`) | not run (the QA lead's re-gate; the fault-scale run exercises the same path) | - | - | NOT-RUN |

api logs of the fault run: 0 tracebacks; 4 uvicorn `ERROR: ASGI callable returned without completing response.` lines, one per
too-slow client whose closing send hit the 1 s bound (the documented cost of closing such a connection from ASGI).

## 3. Behavior changes that need an owner or spec note (not in docs/modules: outside my paths)

1. `SseResponse` (api, integration.md §SSE): `on_close` runs on every path and before the closing body; after
   `client_too_slow` the closing send is bounded (1 s), then uvicorn closes the connection and logs one ERROR line.
2. resumeTurnEvents (spec-09 §3, IMPL-036): V(turn) before the 501, through getTurn's lookup, so a running turn that is
   not stored yet is 404 like getTurn.
3. Cross-pod streaming attach with the mirror unavailable (IMPL-021): waits up to the request deadline (createTurn
   default 120 s; 504 `deadline_exceeded` past it), then 410, or 500 retryable when the original ended unstored.
4. Control characters (R7): path ids 404 (operation's code), query string parameters 400 `schema_violation`, session
   metadata holding U+0000 400 (the contract has no such bound: a schema-valid request can now be 400).
5. watchFile (spec-07 §3.1 step 7, contract): 32 open streams per project and process, then 429 `rpm_exceeded` +
   `retry-after: 5` (watchFile's only declared 429 code; the contracts owner may prefer adding `queue_timeout`); the
   row is re-read every 30 s while the event channel is live instead of every 2 s.
6. session.delete (spec-03 §5, IMPL-077): the session's turn and decision records expire at once with their inline
   body cleared; a large S3 body lives until the next hourly sweep. Upload records carry no resource id and are not
   covered (their 202 FileObject holds the display name; it expires with `platform.idempotency.ttl_s`).
7. createDecision (IMPL-032): the deadline also bounds the file checks: 504 before dispatch.

## 4. Not fixed here (reason, proposed fix)

- QL-3 (the SSE queue ends one ~4 KB frame past 1 MiB): `src/noema/streaming/sse_writer.py` `_reader` (engines, not
  in my paths): check `nbytes + len(frame) > queue_bytes` before `push`, or state the bound as "1 MiB + one frame".
- CR item 3b, explicit Redis pool size: `src/noema/composition.py` `_redis_client` (integrator): pass
  `max_connections` (e.g. 256, from settings) through a `redis.asyncio.BlockingConnectionPool` with a short `timeout`,
  so a burst waits for a connection instead of raising MaxConnectionsError.
- Defect 22 for the listeners named in the item: `turns/coordination.py` (`turn_cancel`, `session_released`, uses
  `get_message(timeout=1.0)`), `adapters/resilience/breakers.py` (`breaker_events`; the resilience fixer's path),
  `labeling/registry.py` (calibrator listener), plus `composition._config_watch`. Fix: the loop of
  `identity/revocation.py` (track the last message or PONG; `pubsub.ping()` after 5 s quiet; raise and resubscribe after
  15 s without any reply). The shared helper would belong in `noema.core` (platform).
- R4 metrics: declare in `src/noema/telemetry/registry.py` (fakes-telemetry, frozen) `aghoo_db_pool_size{pool}`,
  `aghoo_db_pool_in_use{pool}` (gauges), `aghoo_db_pool_acquire_seconds{pool}` and `aghoo_db_statement_seconds{pool}`
  (histograms), `aghoo_redis_pool_in_use` (gauge) and `aghoo_redis_command_seconds{command}` (histogram); then
  `Engine` takes the instruments (acquire wait around `pool.acquire`, size and in-use from `get_size()` /
  `get_idle_size()` on acquire and release, statement time from asyncpg's `add_query_logger` or per unit of work), and
  `composition` passes them and wraps the Redis pool. No code was added for it here: without the declarations every
  emitted name is dropped (or an error under `NOEMA_ENV=test`).
- `make qa-contract` red outside my paths: the contracts fixer's finding-18 change (uploadFile `encoding.file.contentType`
  lists image/png ... text/plain) makes schemathesis raise `SerializationNotPossible: Cannot serialize to 'image/gif'`
  in `tests/qa/contract/test_schemathesis_stack.py[POST /v1/files]` and `FlakyStrategyDefinition` in
  `test_schemathesis_stateful.py` (qa-harness's paths). Same fix as I applied to my own
  `tests/integration/api/test_schemathesis_smoke.py`: register a raw-bytes `schemathesis.serializer` for those types.
- idle_in_transaction_session_timeout on the app pool (engines' suggestion for edge #2) was not added: the item's
  command and statement timeouts are in place, and the integrator's `tests/unit/composition/test_background.py:128`
  pins the exact server settings.

## 5. Existing tests changed with the behavior

- `tests/unit/api/test_idempotency_hook.py::test_an_in_flight_streaming_duplicate_attaches_to_the_event_log`: the
  "unattachable -> 500 at once" assertion removed (now the waiting-then-410 test above).
- `tests/unit/identity/test_sessions_routes.py::test_delete_then_the_job_purges_everything`: the frozen fake
  idempotency repo gets a recording `expire_for_session` (monkeypatch), asserted called per unit of work.
- `tests/integration/api/test_schemathesis_smoke.py`: a raw-bytes serializer for uploadFile's file-part media types
  (the contract changed under it in this wave; it failed in `make test-integration` before).

## 6. Files

Source: `src/noema/api/{sse_frame.py,app.py,overload.py}`, `src/noema/api/middleware/{gate.py,idempotency.py}`,
`src/noema/api/routes/{turns.py,decisions.py,files.py}`, `src/noema/db/repos/{_base.py,files.py,sessions.py,turns.py,
decisions.py,idempotency.py}`, `src/noema/identity/jobs.py`, `src/noema/ops/idempotency/{waiting.py,store.py}`,
`src/noema/media/pipeline.py` (file events and `watch` only). Tests: `tests/unit/api/{test_sse_frame.py,
test_turn_route_hardening.py,test_idempotency_hook.py,test_gate_step.py,test_decision_route_hardening.py,
test_watch_cap.py}`, `tests/unit/ops_ledger/test_idem_done_subscriber.py`, `tests/unit/media_pipeline/test_watch_polling.py`,
`tests/unit/identity/test_sessions_routes.py`, `tests/integration/db_repos/test_nul_ids.py`,
`tests/integration/identity/test_sessions_pg.py`, `tests/integration/ops_ledger/test_idempotency_faults.py`,
`tests/integration/api/test_schemathesis_smoke.py`. Report: this file.
