# Orchestrator (`noema.turns`, `noema.api.routes.turns`) — Wave C1 handoff, C2a adaptation

The turn orchestrator of spec-04: createTurn ingress steps 6 and 8-12, the commit rule, bindings and conditioning,
late decision policy, verification, preflight, context and prompt assembly, persistence and finalize, cancel /
disconnect / abandon, failure handling, and the four turn routes. Model calls happen only inside the decision and
generation engines (Bedrock-only); the orchestrator talks to every other module through the frozen core Protocols.

## Public surface

| Module | What |
|---|---|
| `noema.turns.service.TurnService` | the process entry point. `TurnService.of(services)` (one per process, `services.runtime.local("turns.service", ...)`); `authorize(principal, TurnRequest)` (step 6); `prepare(snapshot=, principal=, request=, authorized=, turn_id=, t_in=, deadline=, trace_id=)` (steps 8-11) -> `PreparedTurn`; `create(snapshot=, principal=, request=, turn_id=, t_in=, deadline=, trace_id=, begin=, keyed=)` (steps 6-11 for the createTurn route, `begin` = step 7; a stored turn of a session without file parts, `retry_of` or Idempotency-Key runs steps 6-10 in ONE unit of work: the session read, the lease and the history read, committed before admission; step 9's `broker.get` is made before the unit opens, after the step-6 role check (403 before any lookup, STS included; Wave E), and its failure raised at step 9's place, so the error order is unchanged; E sprint A) -> `PreparedTurn` or `begin`'s replay; `start(prepared, on_dispatch=)` (step 12: registers `turn_owner`, appends `turn.created`, spawns the turn task; any failure in it releases the reservation, the owner entry and the session lease before re-raising, so the retry is not `session_busy`, C2b/D fix) -> `TurnRunner`; `wait(runner)` (`stream: false`); `get(principal, turn_id)` (getTurn); `cancel(principal, turn_id)` (cancelTurn); `start_background()`; `on_remote_cancel(tenant_id, turn_id)`; `check_abandoned()` (one watchdog pass) |
| `noema.turns.runner.TurnRunner` | one turn: `run()` -> `TurnResult` (always finalizes, also when the runtime cancels it); `request_cancel("cancelled" \| "abandoned" \| "failed")` (a no-op once `outcome_final`); `request_shutdown()` (the drain: `error{all_routes_unavailable}`); `outcome_final` (error sent or `generation.completed` sent); `done` (anyio.Event), `result`, `closed` (`turn.completed` appended), `t_d0` |
| `noema.turns.request` | `parse_turn_request(body) -> TurnRequest` (no Pydantic; the gate validated the body), `RequestPart`, `HistoryOptions`, `DecisionOverrides` |
| `noema.turns.bindings` | `evaluate(bindings, answers, final_names, policy) -> Evaluation`, `matches`, `Tightening`, `policy_for(store_content)`, `content_policy(log_content)` (spec-04 §5.1) |
| `noema.turns.spec` | `compute_spec0`, `decide` (pre-commit keys, IMPL-026, request pins, hard filter, conditioning), `same_spec`, `sticky_json`, `Pins`, `Spec0`, `Decided` |
| `noema.turns.conditioning` | the canonical `decision-conditioning@1`: `qv`, `pct`, `line`, `render`, `signature`, `summary`, `build` -> `core.Conditioning`, `wire` |
| `noema.turns.routing` | `routing_wire(...)` -> openapi `RoutingDecision` |
| `noema.turns.coordination` | `Coordinator` Protocol and `RedisCoordinator(redis, keys, clock)` (FIFO Lua script, `session_pending`, `turn_owner`, `turn_cancel` / `session_released` pub/sub); `CoordinationDown` |
| `noema.turns.session_order` | `acquire(db, coordinator, clock, principal, session_id=, turn_id=, expected_seq=, queue_timeout_ms=)`, `acquire_then(..., then=)` (the same, with `then(uow, lease)` run in the unit of work that takes the lease: step 10's history read at the locked `last_seq`; its failure rolls the lease back), `try_once`, `release`, `renew` (spec-04 §2) |
| `noema.turns.ingress` | `authorize`, `plan_turn` (step 8), `prepare` (steps 9-11), `verify_possible`, `visible_to` |
| `noema.turns.preflight` | `run_preflight(files, estimator, media_budget, PreflightInputs) -> Preflight` (`history_texts`: the older media's text derivatives), `page_texts` (whole `markdown` / `ocr_text` derivatives split on `\f`, read concurrently; None when neither is readable), `modalities_of`, `ingress_blocks`, `estimate` |
| `noema.turns.context.budget` | `candidate_profiles`, `context_budget` (C, O, R), `attempt_max_tokens`, `worst_case_out`, `profile_usable` (hard filter), `member_aliases` |
| `noema.turns.context.history` | the stored `input_parts` format, `HistoryTurn`, `MediaText` (an older media part's text derivative), `committed_turns`, `select_history`, `decision_context`, `HISTORY_SCAN_LIMIT` (64) |
| `noema.turns.prompt` | `PREAMBLE_V1`; `TurnPrompt(PromptInputs, negotiator)` with `.strict` and `.for_run(conditioning)` -> `RunPrompt` (the `PromptBuilder` of one profile run); `untrusted(label, kind, text)`. Escaped history and current text are built once per spotlight mode, media once per alias (parts negotiated concurrently) |
| `noema.turns.requests` | `router_request(runner, preflight, *, escalate_until)`, `verification_request(runner)` -> `(DecisionContext, DecisionRequest)` |
| `noema.turns.concurrent` | `gather(calls)`: a small fan-out (one call inline, else a plain task group) |
| `noema.turns.persist` | row mapping (`attempt_rows`, `stored_content`, `finalize_payload`), `usage_lines`, `usage_wire`, `usage_sum`, `line_id`, `finalize(...)` (transaction + outbox fallback), `pg_text` (stored input and output carry U+FFFD for U+0000, which Postgres text and jsonb reject: such a turn failed its finalize and then poisoned the outbox; Wave E) |
| `noema.turns.finish` | `finish(runner)`: spec-04 §10 steps 2-7 |
| `noema.turns.views` | `turn_object(...)` (in memory), `stored_turn_object(turn, attempts, decision, verification)` |
| `noema.api.routes.turns.router` | `POST /v1/turns` (createTurn), `GET /v1/turns/{turn_id}` (getTurn), `POST /v1/turns/{turn_id}/cancel` (cancelTurn), `GET /v1/turns/{turn_id}/events` (resumeTurnEvents: 501 after authz) |

## Wiring (composition.py, C2)

- The router is already listed in `noema.api.app.API_ROUTERS`; nothing to mount.
- `TurnService.of(services)` builds `TurnService(services, RedisCoordinator(services.redis, services.keys,
  services.clock))` lazily inside `services.runtime`. It reads these `Services` fields: `clock`, `ids`, `config`
  (only through the snapshot the ingress pinned), `broker`, `planner`, `admission`, `estimator`, `cost`, `outbox`,
  `db`, `events`, `sse` (route), `files`, `negotiator`, `media_budget`, `sampler`, `compiler`, `decisions`,
  `generation`, `authz`, `telemetry`, `redis`, `keys`, `runtime`, `settings` (`pod_id`). `services.redis` must be the
  bytes client (`decode_responses=False`).
- In `composition.start(services, "api")` call `TurnService.of(services).start_background()` (it otherwise starts with
  the first turn). It spawns `turns.cancel_listener` (`turn_cancel` + `session_released` pub/sub, reconnects with a
  1 s backoff), `turns.owner_refresh` (every 20 s), `turns.watchdog` (every 5 s) and registers the drain hook
  `turns.drain(grace_s)`: running turns continue for the grace period, then `request_shutdown()` stops them with
  `error{all_routes_unavailable, retryable: true}` (status `failed`, spec-11 §7 step 3), and the hook returns once
  they finalized (at most `SHUTDOWN_STOP_S` = 5 s more). So in `composition.stop` call
  `runtime.drain(timeout_s=grace_s + 10)` before `Outbox.flush` (request 14). A turn still running at
  `runtime.close()` finalizes anyway (shielded, at most `FINISH_LIMIT_S` = 10 s), then the cancellation propagates.
- Tests inject another coordinator: `runtime.local("turns.service", lambda: TurnService(services, coordinator))`
  before the first request (see `tests/unit/turns/harness.py`).
- The real engines must pass the conformance kits; the orchestrator reads `ProfileRun.upstream_started` (core
  Protocol since C2a) for `decision.completed.arrived`.

## Config keys read

`routing.{policy_version, router_profile, default_profile, uncertain_profile, intent_routes, bindings,
speculation.{max_hold_ms, slack_ms, restart_on_conditioning_change}, verification.{decision_profile, questions}}`;
`tenants.<t>.{allowed_models, request_overrides.{decision_profiles, generation_profiles}}`;
`decision_profiles.<p>.{id, version, deadline_ms, min_quorum, members[].{model, weight, optional}}` (questions and
`commit_critical` through the QuestionCompiler, `platform_from = routing.router_profile`);
`generation_profiles.<p>.{mode, members[].{model, max_output_tokens}}`;
`models.<a>.{id, context_window, max_output_tokens_cap, reasoning_reserve_tokens, controls.effort}`;
`platform.{sessions.queue_timeout_ms, events.resume_window_s, limits.admission_queue_timeout_ms,
data_classes.<dc>.{store_content, retain_days}}`. `speculation.holdback_ms` is not read (IMPL-003).

## Behavior summary (where each spec rule lives)

- **Ingress** (`ingress.py`): step 6 checks role/scope first (403), then the session (404 `session_not_found`, 403
  `session_forbidden`), file parts (visibility V, else 404 `not_found`) and `retry_of` (404). The route calls
  `begin_idempotency(resource_id=turn_id, stream=)` (step 7) after step 6. Step 8 (`plan_turn`): `strategy`
  speculative/hedge 422; `decisions.profile` allowlist 403 (`details.allowed`); compile; platform question above
  L 422 `unsupported_parameter` (`param: decisions.profile`, `details: {max_choices, question}`); `decisions.min_quorum`
  422; pinned `generation.profile` 403 / best_of_n·fuse 422 / effort the primary member rejects 422; `verify: true`
  without verification 422; file status 409 unless `wait_for_files` (`allow_partial_files` drops rejected parts).
  Step 9 `broker.get` (503). Step 10 session lease + history at the locked `last_seq` (`store: false`: no lock, the
  current `last_seq`, 409 `session_conflict` on `expected_seq`). Step 11 estimate, work graph (router, verification
  when possible, generation over the candidate profiles), `admission.reserve(deadline=ctx.deadline)`. Any error after
  the lease releases it.
- **Commit rule** (`runner.py`): t_d0 = the instant `DecisionEngine.run().__aenter__` returned (stage-0 views
  resolved, IMPL-023/074); W and `decision_ms` count from it (`escalate_until` is fixed before the entry, so it is
  at most W, request 10). A1 on spec0 (held) from t_d0; S1 with the W cap (`(c)`: `drun.cancel()` at W);
  `(b)` hold timer at `first_text() + max_hold_ms`; equal specs -> `commit("spec_agrees")` (armed before text);
  different -> `cancel("spec_changed")` then A2 (`hold=False`, at most one restart); gate -> one run after S1.
  (a)/(c) is applied once both S1 is decided and A1 is registered, whichever comes last (S1 may resolve while
  A1's entry awaits credentials).
  `route.selected` waits for the run's first `generation.started` (it needs that attempt id); `route.confirmed` is
  appended immediately before `generation.committed`. The final snapshot is bounded by `max(W, now) + 2 s` (then
  `drun.cancel()`), so a turn never hangs on its decision.
- **Late policy**: the final snapshot re-runs `evaluate`; only tightening keys take effect (`verify_answer`,
  `log_content`, `response_cache`, `spotlight: strict` for attempts that start later, `disable_tools`); stored
  `matched_bindings` = S1 ∪ final.
- **Stop paths**: cancel/abandon/too-slow (`client_abort` frames), deadline (no frames, `error{deadline_exceeded}`),
  lost lease (`error{internal_error}`), shutdown (`client_abort` frames, `error{all_routes_unavailable}`), a failed
  watcher task (`error{internal_error}`); each cancels the active run, waits for its events, then emits the error
  and `drun.cancel()` (generation frames precede the late decision frames). The first outcome wins: a cancel,
  abandon or too-slow after the turn's error or after `generation.completed` changes nothing (status stays
  `failed` / `completed`; cancelTurn answers `completed`). A run's AttemptRecords are collected also when its event
  loop failed (the run is cancelled first) or the task was cancelled (after the engine's exit), so usage lines and
  attempt rows are never lost; the session-lease renewal treats any DB error as still held (logged). The renewal
  runs every `RENEW_EVERY_S` = 10 s, the first one due 10 s after t_in (the lease is taken at step 10, before the
  admission wait, so after a wait over 10 s it renews as the turn task starts; Wave E). A cancel, abandon, lease loss
  or shutdown that lands while the router decision's entry is still resolving cancels the run as soon as the entry
  returns, so no member call goes out (Wave E).
- **route.selected**: exactly one per turn (P-SSE-1). A turn that stops before any run started an attempt
  (preflight failure, a failing decision entry, a cancel in preflight) gets spec0's (a minted `attempt_id`, no
  `generation.started`) before its `error` / `turn.completed`; `decision.completed` is owed only once t_d0 was
  reached.
- **Finish** (`finish.py`): status, content policy = the strictest of `log_content` and the data class, the
  finalize transaction (`SessionRepo.finalize_turn` / `TurnRepo.insert`; `LeaseLost` -> nothing stored and
  `error{internal_error}`; DB failure -> `session_pending` then `Outbox.put(turn_finalize)`; a transaction still
  running after `persist.FINALIZE_BUDGET_S` = 5 s, i.e. a hung rather than refused Postgres, is cancelled and is such
  a DB failure, whose idempotent outbox apply covers a commit whose reply was lost (Wave E)), one usage line per
  attempt (`generation` / `waste`, id `h(f"{attempt_id}:{line}")`), `reconcile` + `release`, `DEL turn_owner`,
  `turn.completed{usage, timings}`, `seal(resume_window_s)`, labeling offer (stored, input stored). If `finish()`
  fails (a bug) or the shutdown bound cuts it, a minimal `turn.completed` is still appended and sealed and the
  reservation and `turn_owner` are given back, and so is the session lease when the finalize did not run (nothing
  is persisted; an outboxed finalize keeps the lease, which then expires in 30 s; Wave E).
- **Ingress limits**: at most `request.MAX_MEDIA_PARTS` = 64 media parts (file_id and inline images) per turn,
  checked while the body is read, before step 6's file lookups (400 `schema_violation`, `param: input`,
  `details.max_media_parts`; Wave E, the createDecision bound). The data class never fails open: a sessionless `store: true` turn with Postgres down at
  ingress is 500 `internal_error` (retryable); a `store: false` one continues as `internal`. `history.last_n` is
  capped at `HISTORY_SCAN_LIMIT` (64) for the `TurnRepo.recent` read (at least 2, the decision context).
- **Cancel lookups** are tenant-scoped on every branch: the local registry and the finished registry compare the
  turn's tenant with the caller's (404 otherwise, before authz), `turn_owner` carries the tenant, the DB is RLS.
  A `turn_cancel` message for a local turn of another tenant is dropped with the SEC-ISO-02 security event
  `pubsub_tenant_mismatch` (log record, `aghoo_security_violations_total`, an `audit_event` row of the turn's tenant
  through the outbox, `details.channel = "turn_cancel"`; Wave E).
  The finished registry expires by a heap ordered by `expires_at` (O(log n) per finished turn; `store: true` turns
  are kept too: they answer without a DB read and while their finalize waits in the outbox).
- **History media** (IMPL-045): the previous media-bearing turn's media are negotiated natively per alias; older
  media are their text derivatives (image `ocr_text`; document `markdown` with scanned pages from `ocr_text`) as
  `<untrusted_input source="{label}" kind="markdown|ocr_text">` + `datamark(esc(text))`, within what the derived-text
  allowance T leaves after an upper bound of the current and previous media's page texts, newest first, cut at a
  page boundary (then a `[doc-N: pages 1-k of n]` line); counted in `est_input_tokens`; without one (or without
  room): `[label: not available]`. They are read `OLDER_BATCH` = 4 media at a time and reading stops once the
  allowance is used up (none left, or a page that does not fit: an older part never passes a cut newer one), and
  every per-turn fan-out (`concurrent.gather`: derivative reads, the previous turn's `files.get`, negotiation) runs at
  most `GATHER_LIMIT` = 8 calls at once (Wave E: a document-heavy session no longer reads every older derivative,
  all at once, per turn).

## Stored formats defined here

- `turn.input_parts`: `[{"type": "text", "text"}, {"type": "json", "json"}, {"type": "image", "file_id"},
  {"type": "file", "file_id"}]` in part order (all input items flattened); NULL under `metadata_only`. Labels are not
  stored (they are renumbered per turn: current parts, previous media-bearing turn, older turns).
- `turn.redactions`: `{"user": [{start, end, reason}], "assistant": [...]}`, code-point spans of the
  `[REDACTED:<kind>]` markers over the user text (text and JSON parts joined by "\n",
  `HistoryTurn.user_text()`) and over `output_text`. The user spans are computed on the request's JSON key order;
  `jsonb` reorders object keys, so for turns with JSON parts they are advisory (spec-03 §2): readers take the spans
  from the markers in the text they render (identity.md item 2).
- `turn.routing`: the `RoutingDecision`; `route_id` = the served profile, `generation_profile` = the decided one,
  `fallbacks = [route_id]` after an IMPL-016 step-3 fallback. `turn.generation_profile` = `route_id`.
- `attempt.usage`: the openapi `Usage` object (cost fields included); `turn_file`: `(part_index, file_id)`.
- `session.sticky_spec`: `{generation_profile, effort, max_output_tokens, answer_mode, modalities}` (sorted), written
  only when S1 was evaluated; otherwise the finalize writes back the value read with the lease (`lease.sticky_spec`,
  not the step-6 read, which a FIFO wait behind another turn makes stale; Wave E).

## Telemetry

Spans: `turn` (`aghoo.tenant`), `preflight`, `history.fetch`, `bindings.evaluate`, `verification` (`Span.error(code)`
on failures). Metrics: `aghoo_ttft_seconds`, `aghoo_e2e_seconds` (`profile, model, strategy`),
`aghoo_spec_restarts_total{profile}`, `aghoo_spec_agreement_ratio{profile}` (process-local running share),
`aghoo_conditioning_total{answer_mode}` (`aghoo_wasted_output_tokens_total{profile, reason}` is the generation
engine's, counted once per waste attempt; until Wave E the orchestrator added it a second time, so the counter read
2x the waste: `test_waste_counter_is_counted_once_per_waste_attempt`; `aghoo_stuck_attempts_total` is the generation
engine's attempt watchdog, spec-11 §6; the orchestrator only logs attempts without a record). Logs: `noema.turns` (unexpected failures, failed watchers, interrupted and unfinalized
turns, lost leases, lease renewal errors, abandoned turns, forced decision cancels), `noema.turns.persist` (finalize -> outbox), `noema.turns.coordination` (Redis down), `noema.turns.ingress`.

## Error codes raised

HTTP (ingress): `permission_denied`, `session_forbidden`, `session_not_found`, `not_found`, `feature_not_available`,
`profile_not_allowed`, `unsupported_parameter`, `file_not_ready`, `file_rejected`, `tenant_access_misconfigured`,
`session_conflict`, `session_busy`, `no_route_satisfies_constraints`, `internal_error`, plus the compiler's
(`schema_violation`, `unsupported_question_type`, `reserved_question_name`) and admission's (`rpm_exceeded`,
`tpm_exceeded`, `queue_timeout`) passed through. SSE `error`: the engines' codes as given, `deadline_exceeded`,
`internal_error` (lost lease, a failed watcher), `all_routes_unavailable` (shutdown after the drain grace),
`no_route_satisfies_constraints`, preflight's `image_too_large` / `file_rejected` / `file_not_ready` /
`deadline_exceeded`. Ingress `internal_error` also for a sessionless `store: true` turn whose project can't be read. `stream: false` maps the turn's error to its HTTP status (`upstream_stalled`
-> 503 `all_routes_unavailable`); cancelTurn 404 / 500 (Redis down, not local, not stored); resumeTurnEvents 501.

## Tests (spec-10 §3.1 ids)

| Id | Where | Notes |
|---|---|---|
| G01-G11, G03b, G04b, G05b, G10b | `tests/unit/turns/test_golden_sse.py` | real orchestrator on the scenario-driven fakes and the FakeClock: order, exact fields, delta runs, `decision.updated` time, every frame through the ContractValidator (+ `result_invariants`), P-SSE-1, the fixture's DB assertions, G07's cancel answers |
| G01-G11 (real generation engine) | `tests/unit/turns/test_golden_real_generation.py` | the orchestrator with `noema.generation.StreamingGenerationEngine` on the fake adapters: all match (G02/G06 no longer pin `wasted_output_tokens`, generation.md request 2) |
| G01-G11 (real decision engine; both real engines) | `tests/unit/turns/test_real_decision_engine.py` | `noema.decision.DecisionEngineImpl` + `QuestionCompilerImpl` (with and without the real generation engine): all match except G04b's `decision_ms` / `decision_lead_ms` (its `hold_until_cancel` is a fake-only knob: the real engine closes S1 at its stage-0 deadline, 1000 ms, before W) |
| G12, G12b | `tests/unit/turns/test_golden_attach.py` | through the real routes and middleware: R2 attach (same pod / `attach_remote` on a second pod), R3 replay, R4 410, R5 409; byte-identical frames |
| P-SSE-1 | `tests/unit/turns/test_sse_properties.py` | hypothesis over generation timings/faults, decision outages, gate, race pins, cancels, short deadlines, `store: false`, generation entry delays (S1 before A1 registers) and decision entry delays (slow views); `tests.golden.compare.sse_invariants(events, reached_t_d0=runner.t_d0 is not None)` holds as is (60 examples in `ci`; 1,200 explored over 20 seeds) |
| T-BIND-1…5 | `tests/unit/turns/test_bindings.py` | plus decision-unavailable, hard filter, conditioning off |
| T-COND-1…4 | `tests/unit/turns/test_conditioning.py`, forged tags in `test_prompt.py` | T-COND-1's block, summary and signature come from `tests/golden/conditioning/T-COND-1.json` |
| T-SESS-1…6 | `tests/integration/turns/test_session_order.py` and `tests/unit/turns/test_session_order_unit.py` | T-SESS-1…5 on the real Postgres `SessionRepo` (pg fixtures, noema_app, RLS) and valkey; two pods, each with its own Postgres pool and Redis client (true OS processes: C2's compose `app` profile); FIFO contenders join in a fixed order (no timing sleeps); the killed pod's lease is expired in Postgres. T-SESS-6 on the fake world with valkey |
| T-TIME-1…4 | `tests/unit/turns/test_timing.py` | plus T19 (deadline; `finish_reason: cancelled` on the fake and the real generation engine) and ruling R2 (the hold cap with nothing buffered arms the fallback: `holdback_deadline` at its own first text, S1 `after_commit`) |
| T-HIER-T1 | `tests/unit/turns/test_hierarchical_app_question.py`, `tests/unit/turns/test_real_decision_engine.py` | with the fake engine, and with the real engine's two rounds (non-blocking provisional at S1, final in the update; blocking + 3000 ms moves S1 and W; blocking past the default deadline is unavailable); the wire-mock run is C2's |
| T-ING-1 | `tests/unit/turns/test_routes.py` | spec-04 names `integration/api/test_ingress_order.py` (api-owned path); placed in the orchestrator's tree |
| R5 machinery | `tests/unit/turns/test_verification.py` | |
| spec-04 §12 rows | `tests/unit/turns/test_cancel_and_failures.py`, `tests/unit/turns/test_ingress_limits.py` | orchestrator side of F2/F9/F10/F11 (the fault suite itself is QA's, Wave E); cancel after the outcome is final; lease renewal statement errors; a failing watcher; the data class with Postgres down |
| spec-11 §7 steps 3-4 | `tests/unit/turns/test_shutdown.py` | drain: grace, then `error{all_routes_unavailable}` and every turn finalized before the hook returns; short turns untouched; a completed turn stays completed; a forced `runtime.close()` still finalizes (fake and real generation engine); a failing finalize still ends the stream |
| spec-09 §3/§7 tenancy | `tests/unit/turns/test_tenant_isolation.py` | a foreign tenant_admin gets 404 on cancel (local, other pod, finished, stored) and getTurn |

Also: `test_decision_entry.py` (S1 before A1 registers, t_d0 after a slow decision entry, a failing decision
entry, a deterministic `strategy: gate` turn), `test_history_media.py` (older media as text derivatives, page texts,
builder caches and concurrent negotiation), `test_ingress_limits.py` (`last_n` cap, finished-registry expiry),
`test_prompt.py` (assembly, spotlighting), `test_preflight.py`, `test_history_and_content.py`, `test_restart.py`
(conditioning restart, strict spotlight on A2), `test_routes.py` (stream false, getTurn, cancel, 501),
`test_telemetry.py`, `test_concurrent.py` (`gather` under autojump makes no phantom time; first failure cancels the
rest), `tests/integration/turns/test_coordination.py` (the Redis coordinator, Redis down).
Not written here: `integration/turns/test_golden_wire.py` (G01-G12 through the wire mock with the real adapters and
engines: C2), T-FSM-1 (generation's).

Run: `make test MOD=turns` (unit + integration; the integration tests need `make services-up`).

## Known limits

1. (Closed in C2b, request 10.) The router context carries `escalate_slack_ms = speculation.slack_ms` next to the
   conservative `escalate_until`, so the engine counts W from its own t0; that t0 precedes t_d0 only by the L2
   lookup, so the engine's limit is never later than the W armed at t_d0.
2. `session_order.acquire` follows the spec literally (`try_once` first): a newcomer arriving at the exact release
   instant can take the lease ahead of the queue head (FIFO holds among queued waiters).
3. (Closed in C2b.) `timings.holdback_ms` is measured from the committed attempt's own first text
   (`AttemptRecord.first_text_at`) to the commit, so the R2 case reports 0 as spec-04 §10 says. It is null when the
   committed attempt's record was not collected (`COLLECT_S` expired). The commit instant is the engine's:
   max(`runner.commit_asked`, that first text), i.e. 0 for an armed or auto-committing run and the S1 / hold-cap
   request for a buffered answer; `commit_wall` (when the runner's consumer saw `generation.committed`) is later by
   the event's queue hop, which made 19.5% of the 50-stream load turns report 1-2 ms (C2b/D perf review: that hop
   is TTFT overhead, measured there, not holdback).
4. `generation.temperature` and `metadata` are accepted and ignored (no `AttemptSpec` field, no turn column).
5. `aghoo_verification_flags_total` is not emitted (no config says which verifier outcome is a flag).
6. A finished `store: false` turn answers cancelTurn only on its own pod within the resume window (404 elsewhere).
7. `Reservation.reconcile` gets the attempts' usage plus the decision results' member usage (by model id); members
   whose model is not a candidate alias are not reconciled.
8. `store: false` turns with a `session_id` still need Postgres for the step-6 session check (500 when it is down);
   sessionless `store: false` turns run without it.
9. `stream: false` turns cancelled by another request return 200 with the partial TurnObject.
10. Shutdown: the drain hook returns up to `SHUTDOWN_STOP_S` (5 s) after the grace period (request 14). A forced
    `runtime.close()` with turns still running relies on the generation engine ending its attempts on exit (the
    real and the fake engine do).
11. A cancelTurn routed to another pod (`turn_owner`) answers `cancelling` even when that turn's outcome is already
    final; the owner pod then leaves the turn's status unchanged.
12. When `finish()` itself fails, the minimal close stores nothing; it releases the session lease (publishing
    `session_released`) only when the finalize did not run (`runner.finalize_outcome` is None): an outboxed finalize
    still needs it, and that lease expires after 30 s (Wave E).
13. (Closed in C2b.) Preflight emits `active_content_removed` (`from` = the file's media type, `to: pdf_sanitized`,
    the file's first `part_index`) once per current-turn file whose `pdf_sanitized.active_content` is non-empty.
    Previous-turn media re-sent natively get none (the previous turn reported it, and `part_index` names a part of
    this turn's input).
14. Two guards stay as defenses for any engine: a run `error` that arrives once `ctx.deadline` has passed becomes the
    turn's `error{deadline_exceeded}` (T19), and the final snapshot wait is bounded by `max(W, now) + 2 s`.

## C2a adaptation (2026-10-10)

- `runner.arrived()` reads `ProfileRun.upstream_started` directly; the `getattr` probe, its first-text fallback and
  `tests/unit/turns/harness.py::ProbedGeneration` are gone (FakeProfileRun's transitional `upstream_started` setter
  was removed in C2b).
- `noema.turns.concurrent.gather` is a plain task group (the FakeClock no longer makes phantom time).
- Tests: `harness.TurnValidator` is gone (plain `ContractValidator`); `TurnHarness.override(name, value)` swaps a
  service; `sse_invariants(..., reached_t_d0=)` replaces the local corrections; the G02/G06 waste exemptions and the
  `tests.unit.decision.scenario_compat` dependency are gone; T-COND-1 loads its golden vector.
- `routes/turns.py` keeps reading `deps.get_context/get_services/get_principal` directly (no dependency resolution
  on the hot path); the async `*Dep`s would give the same values.
- No other C2a interface change touches this module: it never calls `CostMeter.price` (generation and decision
  price their own usage), builds no `PlannedPart`, negotiates media parts only and leaves `attempt_id` stamping to
  the generation engine.

## C2b residuals (2026-10-10)

- `requests.router_request` sets `DecisionContext.escalate_slack_ms` (known limit 1 closed); verification keeps
  `escalate_until=None`. `test_t_d0_is_after_the_decision_entry_so_w_counts_from_resolved_views` checks both fields.
- `finish.timings` reads `holdback_ms` from the committed `AttemptRecord.first_text_at` and `runner.commit_wall`
  (known limit 3 closed; `test_hold_cap_with_nothing_buffered_arms_the_fallback_ruling_r2` asserts 0).
- C2b/D perf review: the commit instant is `runner.commit_asked` (set where A1 is asked to commit: `_apply_s1`,
  `_hold_timer`) or the first text when later, not `commit_wall` (`commit_wall` only says that something committed);
  `test_timing.py::test_holdback_ends_at_the_engine_commit_not_when_the_runner_sees_it` delays the committed event
  by 3 ms (armed: 0, buffered at 720 with S1 at 930: 210).
- `preflight.active_content_removed` (known limit 13 closed; `test_preflight.py`).
- `test_history_media.py::test_the_real_planner_keeps_preflight_history_labels_for_the_previous_turn`: with the real
  `MediaBudgetPlannerImpl`, the plan's previous-turn labels equal preflight's `history_labels` (media-negotiation
  request 10). Text derivatives are read whole (`page=None`), the only form the real `FileService` accepts for them.
