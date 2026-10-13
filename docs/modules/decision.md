# noema.decision: the decision engine, question compiler, L2 cache and decision routes (owner: decision, C1)

Spec: spec-06 (all, including §19 hierarchical), spec-02 §5/§6/§8/§9, spec-11 §2, ADR-003 §1–§4, IMPL-001/002/004/005/
019/025/027 (with the R1 amendment)/028/031/036/047–061/068–072/074–076/079/086. Bedrock only (D1): every member call
goes through `Adapter.complete`.

## Purpose

Answers predicate/choice/score questions with a model ensemble: compiles questions (validation, signatures, the
flat/hierarchical method), renders the exact template prompts, fans out member calls with time-triggered backups,
parses logprob or verbalized distributions, closes answers on quorum/deadline/cancel, pools them (linear pool,
largest-remainder rounding), applies accepted calibrators (stage-bound), escalates weak answers, streams S1/final
snapshots to the turn orchestrator, persists `/v1/decisions` rows and usage lines, and serves the L2 cache, the Jev
view, the OpenAI-compatible facade and `GET /v1/profiles`.

## Public surface

| Symbol | Module | Notes |
|---|---|---|
| `DecisionEngineImpl(deps: EngineDeps)` | `noema.decision` | implements `DecisionEngine`: `run(ctx, req)` (async CM -> `DecisionRunImpl`), `decide(ctx, req)` |
| `EngineDeps(...)` | `noema.decision` | frozen dataclass of the collaborators (below) |
| `QuestionCompilerImpl(config: ConfigRegistry)` | `noema.decision` | implements `QuestionCompiler`; `compile_with(snapshot, ...)` compiles against a pinned snapshot |
| `DecisionCacheImpl(redis, keys, clock, instruments)` | `noema.decision` | implements `DecisionCache` over a bytes `redis.asyncio.Redis` |
| `DecisionRunImpl` | `noema.decision.run` | implements `DecisionRun`; extras: `usage_by_alias()` (for `Reservation.reconcile`), `full_result` (members always), `dispatched` (False on an L2 hit), `record_failure_usage()` (called by the run context after a coordinator bug) |
| `routes.decisions.router`, `routes.openai_compat.router`, `routes.profiles.router` | `noema.api.routes` | createDecision, getDecision, createDecisionOpenAICompat, listProfiles (already in `API_ROUTERS`) |
| pure helpers | `signatures`, `pool`, `confidence`, `hierarchical`, `jev`, `facade`, `profiles`, `escalation`, `elicitation.*` | used by the routes and tests; stable but not Protocols |

`DecisionSnapshot.result` honors `req.include` (`members` / `member_distributions`; `stages` is a no-op); the row's
`result`, `GET /v1/decisions/{id}` and L2 always hold the full result. Turn decisions pass `include={"members"}`.

## Wiring in `composition.py` (C2)

```python
from noema.decision import DecisionCacheImpl, DecisionEngineImpl, EngineDeps, QuestionCompilerImpl

compiler = QuestionCompilerImpl(config)                        # config: ConfigRegistry
decision_cache = DecisionCacheImpl(redis, keys, clock, telemetry.instruments)   # redis: bytes client (decode_responses=False)
decisions = DecisionEngineImpl(EngineDeps(
    clock=clock, ids=ids, adapters=adapters, broker=broker, secrets=secrets, views=views, health=health,
    estimator=estimator, cost=cost, calibrators=calibrators, sampler=sampler, cache=decision_cache,
    keys=keys, db=db, outbox=outbox, telemetry=telemetry,
))
```

- Field names are the `Services` fields (`views` = `InputViewResolver`, `cost` = `CostMeter`, `sampler` =
  `LabelingSampler`, `calibrators` = `CalibratorRegistry`). Nothing is started or stopped: no background task, no
  drain hook. One engine per process; runs are independent.
- `secrets.get("noema/input-digest-pepper")` is read on every run (the provider caches it 300 s); it must exist in
  every environment (the fake world provides it).
- The routes take everything from `ServiceSet`: `authz`, `ids`, `db`, `files`, `planner`, `admission`,
  `estimator`, `compiler`, `decisions`. The facade and `/v1/decisions` reserve with
  `WorkGraphPlanner.plan(decisions=[decision_work(req, ...)], generation=None)` and reconcile/release themselves.
- The turn orchestrator uses only the Protocols: `compiler.compile(router, app, origin="turn", blocking=...,
  platform_from=router)` and `decisions.run(ctx, req)` with `include={"members"}`, `escalate_until=W` and
  `escalate_slack_ms=speculation.slack_ms`.

## Request flow of the routes (spec-04 §1 steps 6–12 applied to decisions)

`authz.check(decision.create)` -> session access (`uow.sessions.get`, 404 `session_not_found`, 403) and the data
class (session, else `projects.get(...).default_data_class`, else `internal`; a failed project read (Postgres down, a timeout) -> `restricted`, fail closed, Wave E) ->
`begin_idempotency(resource_id=dec_…)` -> profile (`resolve_native` / `resolve_facade`) -> compile (400/422) ->
`min_quorum` (422) -> template family (422) -> `file_id` parts (at most `inputs.MAX_MEDIA_PARTS` = 64 media parts,
else 400 `schema_violation` `details.max_media_parts` before any lookup; each distinct file once: `files.get` and
V(file) = the uploader or its session's owner, else 404 `not_found` like a missing file; 409; Wave E) -> admission (429) ->
inline images (`files.ingest_inline`: 413/409) -> `decisions.run` (503 credentials, 504 dispatch) ->
`mark_model_dispatched` (not for an L2 hit) -> finalize -> reconcile/release -> 503 tenant-access rule -> body.
The facade adds 503 `all_routes_unavailable` (stored, `details.decision_id`, `retry-after: 1`). Every 200
(native, Jev, facade) carries the six `x-ratelimit-*` headers of `Reservation.headers()`, read right after the
debit (`service.execute` returns `(final, full, rate_headers)`; IMPL-033, spec-08 §4: 2xx and 429 only, so the
later 503/504 envelopes do not carry them).

## Run lifecycle (spec-06 §10)

- `__aenter__`: request checks (profile version 422, `min_quorum` vs Qmax 422, verify-dist family 422),
  `broker.get` once (503 `tenant_access_misconfigured`), the pepper and the input digest, the stage-0 views
  (concurrent `InputViewResolver.resolve`; text-only input skips it; bounded on the clock at
  `ctx.deadline - 50 ms`: still resolving there -> 504 `deadline_exceeded` at that instant, no call, no row, no
  usage), `t0`, the L2 lookup, then the dispatch check (`ctx.deadline.remaining_ms() < 50` -> 504, no row, no usage)
  and the coordinator task in the run's own task group.
- A question whose non-skipped members cannot make `q_req` (`skipped` by `input_view` or `too_many_choices`) closes
  at stage start with no call: `unavailable / no_capable_member`, the other members `late` with latency 0 and no
  usage (§19.3, §7.3 rule 4; no estimated waste).
- Escalation stages resolve the target's views bounded at `min(escalate_until, deadline) - target.deadline_ms`
  (with `ctx.escalate_slack_ms`, the turn's case, `escalate_until` is replaced by `t0 + stage-0 deadline_ms +
  slack_ms` from the run's own t0, C2b);
  past it, or when the §9.3 budget re-check at stage start fails, the eligible answers stay final as they are
  (`skipped_budget`, not L2-cacheable). No stage is dispatched whose deadline is already past.
- The coordinator owns all state; member units and timers report through an unbounded memory stream; every task is
  spawned from the coordinator task (member spans are children of `decision` / `decision.escalation`).
- Dispatch order (C2b perf smoke): a member call unit first yields `DISPATCH_YIELDS` (6) loop passes, then runs its
  CPU-heavy first step (render, build, sign: about 1 ms each). A turn starts its generation attempt right after the
  decision entry, and that attempt needs about 4 task hops to reach the network; without the yields it went out after
  all 15 router-default calls (+15 ms TTFT). Ordering only: no clock time passes, S1 is about 1-2 ms later
  (`tests/unit/decision/test_dispatch_order.py`). Since E sprint A a member call costs about 0.25 ms of CPU (the
  adapters' lean unary path), so the yields now buy less; they are unchanged.
- A member whose backup cannot fire (no `backup_after_ms`, or it does not fit the round's budget) runs its one call
  in the unit's own task: no hedge task group, memory stream or extra task (E sprint A; the same outcomes, records
  and cancellation as the hedged path, which members with a possible backup keep).
- Pricing (spec-11 §2, IMPL-019): every completed or cancelled call is priced with
  `CostMeter.price(alias, ctx.tenant.home_region, usage, snapshot=ctx.snapshot)`, so a cross-region call finds the
  home-region catalog entry and a config activation mid-run does not change the prices. The member's `region` and
  its usage line keep the serving region (`ModelResponse.region`); cancelled calls use the home region.
- Hierarchical expansion (§19.6, R1): `E` is the minimal prefix of groups by emitted units (ties -> declared order)
  that covers >= 9,000 units **and** holds >= 2 options in total, at most 3 groups (`expand(pool, gids, sizes)`). A
  lone dominant one-option group therefore also expands the next group, and the answer is scored with every option
  of both groups as candidates (one round-2 call per member, for the larger group).
- `commit_critical()` = S1 (`quorum` while a non-blocking answer is open; final otherwise); `updates()` yields at
  most the final snapshot; `cancel()` closes open answers (pending members `late`) and waits for the final result.
  A run so cancelled (`coordinator.cancelled`: answers closed with members in flight, or a pending escalation) is
  never put into L2, even when the rest made quorum (`completed`): its result is truncated, not what a fresh
  computation gives (IMPL-004's rationale; Wave E put gate, `test_t_l2_1_a_cancelled_run_is_never_put`).
- `finalize()` (idempotent, shielded, one 2 s budget scope on the clock): usage lines, member-status metrics, the
  L2 put, and only when `ctx.turn_id` is None the row insert (`uow(principal).decisions.insert`). After the budget
  scope (sync, so a stalled store cannot cancel it): a row that was not committed (insert error, or a DB or L2 put
  still hanging at 2 s; the abandoned transaction rolls back) goes to the outbox as `decision_row` (IMPL-022; the
  apply is `ON CONFLICT DO NOTHING`); a committed row is offered to labeling (purpose decision/router with stored
  input). An outboxed row is never offered (spec-06 §16: the job must not see a missing row). Turns get
  `snapshot.row` (iff `ctx.store`) and `row_for(policy)`. The row's `questions`, `input`, `result` and
  `safety_identifier` carry U+FFFD for U+0000 (`repo.pg_text`: Postgres text and jsonb reject it, so the insert
  failed and the outboxed row could never apply; Wave E).
- `__aexit__` (shielded): `cancel()` + `finalize()` if the caller did not, then leaves the task group. An outer
  cancellation (client abort) still yields the row and every usage line (T-RUN-2). After a coordinator bug (500
  `internal_error`), in-flight calls are stopped and estimated, and `record_failure_usage()` writes the usage lines
  of every dispatched call (no row, no L2).

## Config keys read

`tenants.<t>.{aws_account_id, home_region, defaults.decision_profile, defaults.generation_profile,
request_overrides.decision_profiles, request_overrides.generation_profiles, allowed_models}`;
`decision_profiles.<id>.{version, deadline_ms, min_quorum, members[model, elicitation, template, weight, effort,
optional, backup_after_ms, latency_p95_ms], aggregation, modality_policy, escalation{to_profile, when{confidence_below,
margin_below, any_refused, unavailable}, questions}, questions, commit_critical}`; `templates.<ref>.max_choices`;
`models.<alias>.{adapter, id, modalities, controls.reasoning, controls.top_logprobs_max, reasoning_reserve_tokens,
max_output_tokens_cap}`; `generation_profiles.<id>.{mode, members}` (listing); `routing.{confidence_bands,
policy_version}`; `platform.decision_cache.ttl_s`; `platform.data_classes.<class>.{l2_cache, store_content,
retain_days}`; `platform.limits.admission_queue_timeout_ms`; `labeling.sample` (through the sampler). Code
constants (versioned with the engine, not config): templates, `VIEW_BUDGET_V1`, `MIN_DISPATCH_MS` 50,
`HIER_EXPAND_UNITS` 9,000 (with >= 2 options in `E`, R1), `HIER_MAX_EXPANDED` 3, `HIER_ROUND1_SHARE` 0.5, L2 timeout
20 ms, finalize budget 2 s. `EFFORT_RESERVE` is the shared `noema.config.models.EFFORT_RESERVE` (C2b; also read by
labeling and admission).

## Telemetry

- Spans: `decision` (`aghoo.profile`, `aghoo.profile_version`, `aghoo.decision.cache_hit`,
  `aghoo.decision.calibration_status` = calibrated|uncalibrated|mixed); `decision.member` per call
  (`aghoo.member.id`, `aghoo.member.question`, `aghoo.member.elicitation`, `aghoo.member.input_view`,
  `gen_ai.request.model`, `aghoo.member.backup`, `aghoo.member.status`, `aghoo.decision.round`/`aghoo.decision.group`
  for hierarchical calls, `error.type` = member error code); `decision.escalation` (`aghoo.escalation.from|to|questions`).
  No prompt text, distribution or model output is ever an attribute or a log field.
- Metrics: `aghoo_decision_seconds{profile, stage=quorum|all, member=""}`; `aghoo_decision_quorum_reached_seconds{profile}`
  (first time every commit-critical answer, or every answer outside turns, met quorum);
  `aghoo_decision_member_status_total{member=<alias>, status}` (at finalize); `aghoo_decision_parse_errors_total{member=<alias>, code}`;
  `aghoo_escalations_total{from_profile, trigger=<first fired, canonical order>, outcome=ran|skipped_budget|skipped_too_many_choices}`
  (one per question; `ran` when the stage starts or its preparation failed, `skipped_budget` also when the
  target's views outlast the budget); `aghoo_decision_hierarchical_total{profile, outcome, reason}`; `aghoo_cache_hits_total{layer="l2"}`
  and `aghoo_cache_misses_total{layer="l2", reason=bypass}` (engine), `reason=miss|error` (`DecisionCacheImpl`).

## Error codes raised

400 `schema_violation` (compile rules, facade names/collisions/images), 400 `model_not_available` (facade),
403 `profile_not_allowed` (`details.allowed`), 403 `permission_denied` (authz), 404 `not_found` (getDecision,
file parts), 404 `session_not_found`, 409 `file_not_ready` / `file_rejected`, 413 `image_too_large` (ingest),
422 `unsupported_parameter` (stale pin `details{requested, active}`, `min_quorum` `details.max`, verify family,
hierarchical on jev/facade `details{max_choices, method}`, turn platform question `details{max_choices, question}`),
429 from admission, 503 `tenant_access_misconfigured` (credentials at entry; or all answers unavailable with every
member `tenant_access_misconfigured`, `details.decision_id`), 503 `all_routes_unavailable` (facade, stored),
504 `deadline_exceeded` (dispatch check, or stage-0 views still resolving 50 ms before `ctx.deadline`),
500 `internal_error` (a coordinator bug; waiters never hang; the dispatched calls keep their usage lines).
Member-level codes: `throttled`, `upstream_error`, `invalid_request`, `tenant_access_misconfigured`,
`circuit_open`, `model_not_allowed`, `no_code_token`, `parse_error`, `missing_options`, `unknown_options`,
`value_out_of_range`, `sum_out_of_range`, `reasoning_only`, `empty_output`, `censored_tail`.

## Tests (spec-10 §3.1 decision ids, all covered)

`make test MOD=decision` (unit + integration, 155 tests, < 15 s). Vectors: T-SIG-1 (`test_signatures.py`), T-LP-1,
T-VB-1 (`test_parse.py`), T-POOL-1/2, T-PRED-1 (`test_engine_pool.py`), T-CLS-1, T-OD-1 (`test_classify.py`),
T-CAL-1…4 + binding negatives (`test_calibration.py`), T-ESC-1…3 (`test_escalation.py`), T-RUN-1/2 (`test_run.py`),
T-JEV-2 (`test_jev_facade_censored.py`), T-JEV-3, T-FAC-1 (`test_facade.py`), T-FAC-2
(`test_facade_non_pattern_name.py`), T-L2-1…3 (`test_l2.py`), T-HIER-1…10 (`test_hierarchical.py`); Q3
(`test_jev_property.py`: hypothesis over native choice, score (K in {2, 3, 5, 16}) and predicate answers from
`flat_scored` with member errors and censored logprob tails; the Jev map, choice, score and confidence are checked
against the IMPL-049 formulas recomputed in the test, plus `jev_invariants`), `test_jev_k16.py`. R1 (T-HIER-3):
the `expand()` table with group sizes, the lone dominant one-option group scored on the engine, and
`decision-result.valid-hierarchical-lone-group.json` reproduced from its request (`test_hierarchical.py`). C2a:
home-region and pinned-snapshot pricing (`test_run.py`), views that resolve at once cost no virtual time
(`test_inputs.py`). Review regressions: no call when quorum is impossible (`test_hierarchical.py`,
`test_inputs.py`), bounded stage-0 and escalation views (`test_inputs.py`), coordinator-bug usage lines, stalled
and failed row inserts with no labeling offer (`test_run.py`), `x-ratelimit-*` on every 200 and the route-level
504 (no row, no usage, reservation released, the same Idempotency-Key re-executes; `test_routes.py`); integration: Q2 `test_facade_openai_sdk.py` (official `openai` client
over uvicorn on 127.0.0.1:0, 429/503 retries), Q2-TS `test_facade_openai_ts.py` (the unmodified `openai` npm client
of `sdk/typescript/test/facade.openai.live.test.ts` via `npx --no-install vitest` against a fresh local server; every
vitest case must pass, none skipped; skipped when npx or vitest is missing; ~5 s), Q6 `test_calibration_binding.py`, SE3 `test_l2_isolation.py` (Valkey),
SE8 `test_no_filenames.py`. Reproduced from their requests: `decision-result.valid.json`,
`.valid-hierarchical.json`, `.valid-hierarchical-lone-group.json`, `.valid-censored.json`, the answer and stage of `.valid-escalated.json`,
`decision-jev-response.valid.json` / `.valid-k16.json` answers, `openai-response.valid.json` /
`.valid-non-pattern-name.json`. The golden member requests (`tests/golden/decision_requests`) are byte-identical
(`test_render.py`). Conformance registrations (`tests/unit/decision/conformance_impls.py`,
`tests/integration/decision/conformance_impls.py`): `decision_engine:real`, `question_compiler:real`,
`decision_cache:redis-double`, `decision_cache:valkey`. Every route test runs with the strict response validator.

## Known limits and deviations

- **Resolved in C2a** (`.orchestration/requests/decision.md` items 1-3): the scenario fake reads the
  `<app_question>{` block itself, so the decision tests no longer patch it (the `scenario_compat.py` shim was
  removed in C2b; nothing imports it). A lone one-option group is scored (R1, above).
  The FakeClock no longer jumps while a task group exits; the stage views are bounded by a plain `deadline_scope`.
- **Same-instant coalescing**: S1 and the "quorum impossible" closure (spec-06 §7.2 case 2) wait 1 µs on the
  injected clock so they include member results of the same instant (deterministic under `FakeClock`, required by
  the shared conformance suite; negligible in real time). Case 1, deadlines, cancel, and a quorum that is
  impossible before any call close immediately.
- A row that went to the outbox is not offered to labeling (that decision is not sampled; the outbox has no
  "offer after the row is applied" record kind in Phase 1).
- A hierarchical member whose round-2 call fails stops its other round-2 calls (they become `late`, estimated, on
  `waste`); its status is the failure (`refused` > `timeout` > `error` > `late`).
- A hierarchical artifact returned by the registry is ignored (Phase 1 defines no hierarchical transform; logged).
- During an escalation stage, S1 shows the last closed stage answer for that question (never a partial stage).
- `/v1/decisions` and the facade read the project's `default_data_class` per request (one indexed read).
- (Closed in C2b, orchestrator request 10.) With `DecisionContext.escalate_slack_ms` set (turns) the engine uses
  `min(t0 + deadline_ms + slack_ms, ctx.deadline)` from its own `t0` instead of the caller's conservative
  `escalate_until` (`test_escalate_slack_ms_counts_the_budget_from_the_engine_t0`).
- Breaker outcomes (C2b, adapters request 7): a failed member call records `throttled`, `timeout` (`timeout:*`) or
  `error` (retryable / fatal) in the shared breaker, and nothing for `tenant_access` or `invalid_request`, so one
  tenant's broken grant cannot open the per-(model, region) breaker for every tenant
  (`test_tenant_access_and_invalid_request_are_not_breaker_signals`). A member cut by the caller's deadline
  (`timeout:deadline`, or any failure once `unit.deadline` passed) records nothing (C2b/D integration fix, as in
  generation): it is the caller's budget, not a provider timeout (design 04 §2), and a breaker opened by it would
  reach every pod through `breaker_events` (a model a little slower than the 1,000 ms router deadline, or one pod's
  event-loop lag) (`test_a_member_cut_by_the_deadline_is_not_a_breaker_signal`). An `ok` member records no TTFT
  sample (Wave E): a unary call's whole latency grows with the tenant's input and reasoning, so it fed the shared
  breaker's p95-TTFT rule with tenant-shaped values (one tenant's few slow calls opened the circuit for all;
  `test_unary_member_latency_is_not_a_ttft_breaker_sample`).
- Member output that the parser cannot handle (nested past the recursion limit, or any parser exception) is that
  member's `error{parse_error}` (counted in `aghoo_decision_parse_errors_total`), never an adapter error (Wave E).
- Turn-side profile resolution (`decisions.profile` allowlist, spec-06 §2.4) belongs to the orchestrator (it cannot
  import this package); the compiler enforces the reserved names and the platform-question limit.
- Live smoke items L5/L6 (spec-10 §6) live in `tests/live/` (adapters/QA paths) and need the C2 composition; none
  was written or run here. No AWS resource was touched; all tests use fakes or the compose Valkey.
