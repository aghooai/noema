# 11 — Ops: admission, cost and usage, outbox, jobs, idempotency, retention, reapers, health

Wave A, lead architect. Owners:
- ops-admission: `ops/admission/`;
- ops-ledger: `ops/{cost,prices,usage,jobs,outbox,idempotency,retention,reapers,notify_bridge}`, `TokenEstimator`, `routes/{usage,health,metrics}.py`.

Resolutions: IMPL-016, IMPL-019, IMPL-020, IMPL-022, IMPL-027 (hierarchical reservation), IMPL-029c, IMPL-040, IMPL-077, IMPL-078, IMPL-085.

## 1. Work graph and admission (ops-admission; 02:L114, 04:L276-277)

### 1.1 `WorkGraphPlanner.plan` (sync, pure)

Every count is the **worst case** of one request. Inputs are the spec-02 §8 `DecisionWork` / `GenerationWork` values (`DecisionWork.questions` lists `QuestionWork(name, k, source)` in compiled order, so `Q_k` can filter platform questions by name); the caller builds them:
- turns: one `DecisionWork` for the router decision (`kind="decision_member"`, profile = `decisions.profile ?? routing.router_profile`, K of the platform questions of `routing.router_profile` then the app questions, `escalate_budget_ms = deadline_ms_eff + speculation.slack_ms`), one for verification when `verify_possible` (`kind="verification_member"`, `routing.verification.decision_profile` and its `questions`, `escalate_budget_ms=None`), and a `GenerationWork`;
- `/v1/decisions` and the facade: one `DecisionWork` (`escalate_budget_ms = ctx.deadline.remaining_ms()`), no generation, `streams = 0`.

`verify_possible` = `request.verify is True` or any `routing.bindings[i].then.verify_answer` is true (static; `verify: false` never removes it, IMPL-024). The candidate profiles (orchestrator) = the pinned `generation.profile` if the request pins one, else spec0's profile ∪ every profile a binding or `intent_routes` value can select ∪ `routing.default_profile` ∪ `routing.uncertain_profile`, filtered by the tenant allowlist; `routing.default_profile` is always included (the failure fallback, IMPL-016).

```text
decision stage s with profile P (stage 0: deadline d0 = stage0_deadline_ms; stage k ≥ 1: d = P_k.deadline_ms):
  calls_s(m)   = Σ_{q ∈ Q_s} c(q) × (1 + backup(m))   for every member m of P (optional members and skippable members included)
  c(q)         = 1 (flat) | 1 + 3 (hierarchical: K(q) > L(P) for a choice question; one round-1 call plus at most 3
                 expanded groups, IMPL-027, spec-06 §19.9); only stage 0 can be hierarchical (escalation needs K ≤ L(P_k))
  backup(m)    = 1 iff m.backup_after_ms and m.latency_p95_ms are set and backup_after_ms + latency_p95_ms ≤ d   (spec-06 §6.3)
  Q_0          = every question;  Q_k = the questions of Q_{k-1} eligible for P_k: escalation != "off", P_{k-1}.escalation
                 exists, platform questions filtered by P_{k-1}.escalation.questions (request questions always pass, IMPL-068),
                 K(q) ≤ L(P_k)
  stage k ≥ 1 is reserved iff escalate_budget_ms is not None and P_k.deadline_ms ≤ escalate_budget_ms
                 (any stage may close early, so the bound is per stage, not cumulative; IMPL-025: never in turns by default)
  tokens per decision call = input_tokens + DECISION_PROMPT_OVERHEAD (600, noema.config.models) + max_tokens(call)
                 max_tokens: logprob-codes@2 → 8; verbalized-dist@1 / verify-dist@1 → min(64 + 12·K + R, cap)   (spec-06 §5.4–5.5);
                 for the calls of a hierarchical question, K = max_choices of m's template (the largest a call can carry)
generation (G = the candidate profiles; width(G) = |members| for race, else 1; W = max_G width(G)):
  per alias a:  calls(a, "generation") = max over G that contain a of 4·width(G)     A1, restart, 2 failure attempts (IMPL-016)
  key/project:  generation calls = 4·W  (a turn starts at most 4 attempt waves, whatever the profiles)
  out(m)        = the worst case of the spec-04 §5.1 max_tokens formula over every spec the turn can decide:
                  min(cap_m, max({request max_output_tokens, every binding then.max_output_tokens} ∪
                                 {m.max_output_tokens ?? cap_m ?? GEN_DEFAULT_MAX_TOKENS (4096)}) + reasoning_reserve_tokens_m)
                  (cap_m = models.<m>.max_output_tokens_cap; no cap: no min; undefined set members are dropped; IMPL-084)
  tokens per attempt on m = GenerationWork.input_tokens + out(m)
WorkGraph:
  calls[(a, kind)]   = Σ decision calls on a (per kind) and calls(a, "generation")
  tokens_by_model[a] = Σ over those calls of their tokens
  total_calls        = Σ decision calls + 4·W
  tokens_in          = Σ decision calls × (input_tokens + 600) + 4·W × GenerationWork.input_tokens
  tokens_out         = Σ decision calls × max_tokens(call) + 4·W × max over the candidate members m of out(m)
  streams            = 1 (turn) | 0 (/v1/decisions)
```

Test vectors (`tests/unit/ops/test_work_graph.py`, shipped yaml):
- **T-WG-1** router only: `router-default`, 5 router questions, 0 app questions, no verification, no generation: 5 × (DeepSeek 2 + Luna 1 + Haiku 1) = **20 calls**; the DeepSeek backup applies because 400 + 590 ≤ 1,000. With `decisions.deadline_ms: 900` the backup no longer fits: **15 calls**.
- **T-WG-2** a full default turn (spec0 `sol-fast`, verification possible): decision 20 + verification 2 × (Haiku + Luna) = 4 + generation 4·1 = **28** total calls; per alias: deepseek 10, luna 7, haiku 7, sol 4, opus 4, glm 4 (`opus-deep`, `glm-reason` and `sol-fast` are candidates; `race-sol-glm` and `best-of-sol-opus` are not reachable).
- **T-WG-3** `/v1/decisions` with `lightweight-model-decide`, 3 request questions, D = 120 s: stage 0 = 3, `router-default` = 12, `router-plus` = 12 (1,000 and 2,500 ≤ the remaining deadline): **27 calls**; `escalation: "off"`: **3**.
- **T-WG-4** a turn with `escalate_budget_ms = 1,050`: `router-plus` (2,500) is not reserved.
- **T-WG-5** hierarchical (spec-06 §19). `/v1/decisions` with `lightweight-model-decide` and one 32-option question (`decision-request.valid-hierarchical.json`, `deadline_ms` 3,000):
  - stage 0 = 1 member × (1 + 3) = **4 calls**, each costed at K = 26;
  - `router-default` is not reserved, because K = 32 > 16. So the total is **4**, and `reconcile` refunds 1 (two groups expanded).
  - On `router-default` with a 40-option question: DeepSeek 4 × 2 (backup, 400 + 590 ≤ 3,000) + Luna 4 + Haiku 4 = **16 calls**. `router-plus` is not reserved (K > 16).

### 1.2 `Admission.reserve` and `Reservation`

GCRA per bucket (one Lua script, `KEYS[]` = the buckets of the request, all via `RedisKeys.gcra(bucket)`):

```text
limit L per 60 s; emission interval T = 60 000 / L ms; burst τ = 60 000 ms (one minute of capacity)
debit n:  tat' = max(tat, now) + n·T;  allowed iff tat' − now ≤ τ;  if allowed: tat = tat'
retry_after_s = ceil((tat' − now − τ) / 1000) (≥ 1); n > L can never pass → reject with retry_after_s = 60
refund r: tat = max(now, tat − r·T)
remaining = floor((τ − max(0, tat − now)) / T); reset_s = ceil(max(0, tat − now) / 1000)
```

- Buckets, all debited atomically (all-or-nothing in one script) — RPM with `total_calls`, TPM with `tokens_in + tokens_out`:
  - `key:{key_id}:rpm|tpm` (`limits.rpm_per_key`, `tpm_per_key`; tenant override first, then `platform.limits`);
  - `prj:{tenant}:{project}:rpm|tpm`;
  - `mdl:{tenant}:{alias}:rpm|tpm` for every alias in the graph that has `tenants.<t>.limits.model_quotas[alias]`, with `calls`/`tokens_by_model` of that alias.
  - (not part of `reserve`) `authfail:{client_ip}`: the per-IP bucket on 401s used by identity (10/s, burst 50: L = 600 per 60 s with τ = 5,000 ms; spec-09 §2), through the `RateLimiter` Protocol (spec-02 §8: `check` / `debit`, same script and Redis-down fallback).
- **Model-quota buckets are sliding windows, not GCRA** (Wave E2, QL-2; IMPL-078 amendment). A GCRA with `T = 60 000 / L` admits up to `τ/T + L` units in one 60 s window (C1 measured 1.5×), and no τ keeps every window ≤ L while still passing a debit of n > 1.
  - A window bucket counts its units per 1 s slice and admits a debit of n iff the units of the last `QUOTA_SLICES` = 62 slices (the current one and 61 before) plus n ≤ the cap. The cap is L, or `floor(0.8 · L)` for labeling.
  - Units leave 61–62 s after their debit. So every 60 s window of admissions holds at most L units, and so does every 60 s window of calls made up to 1 s after their admission (`QUOTA_MARGIN_MS`).
  - `retry_after_s` = whole seconds until enough of the oldest slices leave for the debit to fit (≥ 1; 60 when n > L). `remaining` = L − the units counted. `reset_s` = until the newest counted slice leaves (after a debit: until the current slice leaves).
  - A refund of r takes them off the slice of their debit (nothing once that slice left the window). The Redis-down fallback divides L and the cap by the pod count (`NOEMA_EXPECTED_PODS × WEB_CONCURRENCY`).
  - Redis: a hash of slice index → units at `RedisKeys.gcra("win:" + bucket)` (TTL 63 s), in the same all-or-nothing `RESERVE` / `REFUND` scripts as the GCRA buckets.
  - Accepted cost: units a reservation holds count until its refund, so with worst-case reservations the sustained rate is below L. C1 measured about 430–445 Sol calls per minute against L = 600: a turn holds 4 Sol units for about 7 s and refunds 3 at reconcile.
  - Open design item: a call sent more than 1 s after its admission (a fallback after a timeout) is counted in its admission slice. Counting consumed units at their send time would make that case airtight too.
  - `key:`, `prj:` and `authfail:` buckets keep the GCRA above.
- Streams: `ZSET RedisKeys.streams(tenant, project)` of `reservation_id → lease expiry (ms)`; the script drops expired members, then adds one iff `ZCARD < concurrent_streams_per_project`. Lease expiry = `ctx.deadline + 30 s` (`reserve(..., deadline=ctx.deadline)`; None = the implementation's maximum turn lifetime), so a dead pod's slot frees itself.
- Rate exhaustion → 429 immediately: `rpm_exceeded` if any RPM bucket fails, else `tpm_exceeded`; `retry-after` = the largest `retry_after_s` among the failing buckets; nothing is debited.
- Stream-slot or bulkhead exhaustion → wait in the per-pod **weighted fair queue** (weight 1 per tenant; deficit round robin over tenants, FIFO within one), retried on every release and every 100 ms, up to `platform.limits.admission_queue_timeout_ms` → 429 `queue_timeout` (`retry-after` 1). Bulkheads (04 §5): per (tenant, alias), the live reservations that include the alias ≤ `ceil(model_quotas[alias].rpm / 6)` (10 s of quota), only when a quota is configured; counted in the same ZSET scheme (`RedisKeys.streams(tenant, "mdl:" + alias)`).
- `Reservation.consume(kind, model)` (sync) decrements the pre-counted `calls[(model, kind)]`. Above zero it is a bug: `aghoo_reservation_overrun_total{kind}` plus an ERROR log (AssertionError under `NOEMA_ENV=test`); the call still proceeds.
- `reconcile(actual)`: refunds `reserved − consumed` calls and `max(0, reserved_tokens − actual_tokens)` per bucket (actual = input + output of the usage map). `release()` removes the stream slot. Both are idempotent.
- `headers()`: the six `x-ratelimit-*` from the RPM bucket with the lowest `remaining` (`-requests`) and the TPM bucket with the lowest `remaining` (`-tokens`), computed after the debit (IMPL-033).
- **Redis down** (a script error or 50 ms timeout): a per-pod local GCRA with each limit ÷ `NOEMA_EXPECTED_PODS` (06:L189), the fail-safe; stream slots counted locally with the same division. When Redis returns, new reservations use Redis again; local state is not merged; it is reused if Redis fails again within τ (60 s), so a flapping Redis cannot hand each pod a fresh local burst per flap.
- **Labeling jobs** (`CallKind` `labeling`, reserved with `system_principal(tenant)`, spec-02 §1): no key or project buckets (it has no key and a tenant-wide project scope); they debit the `mdl:` buckets only, and only while the bucket keeps ≥ 20% headroom after the debit (`LABELING_HEADROOM`), and the alias's bulkhead keeps ≥ 20% of its places free (at most `floor(0.8 · cap)` live labeling places, so a 1-place bulkhead is never taken by labeling); otherwise `queue_timeout` at once (labeling never waits in the fair queue), and the job is retried with the normal backoff (labeling is best effort; an item dropped after `max_attempts` is only logged). This is the "separate, lower-priority bucket" of 04:L276. Labeling jobs build their `WorkGraph` directly (no planner input type) and pass it to `reserve`.

## 2. Cost meter and usage (ops-ledger; IMPL-019, IMPL-020)

- `CostMeter.price(model_alias, region, usage, *, snapshot=None)` (sync): catalog key = `models.<alias>.price_ref` else `models.<alias>.id`, plus `region` (the tenant's `home_region`, never the serving region), newest `effective_from ≤ now` (spec-05 §2), from `snapshot.prices`: the caller's pinned snapshot (decision, generation and labeling pass `ctx.snapshot`, IMPL-019), else `ConfigRegistry.current().prices`. In `Decimal` (28 digits), per million tokens (08:L508):
  `usd = ((input − cached − cache_write)·p_in + cached·p_cache_read + cache_write·p_cache_write_5m + output·p_out) / 10⁶`, rounded half-even to 1e-6 **per call**. `estimated = usage.tokens_estimated` or any component with a non-zero token count is missing or `TBD` (priced 0).
- `TokenEstimator.estimate(alias, system, blocks)` (sync, IMPL-029c): `ceil(chars / chars_per_token)` (4.0) per system string and `TextBlock`; `ImageBlock`: `min(1600, ceil(width·height / 750))`; `DocumentBlock`: `1500 × pages`. Exact for the vectors in `tests/unit/ops/test_token_estimator.py`; the 2% accuracy gate is live/Phase 0 only.
  - *Waves E2–E3 (IMPL-029c amendments):* `chars_per_token` and a per-request overhead are per model alias (`TOKEN_ESTIMATOR_V1`, fitted on the Wave E live calls by `python -m noema.ops.cost.estimator_fit`; an alias not in it: 4.0, no overhead). `NOEMA_TOKEN_ESTIMATOR_TABLE` (spec-05 §5) replaces the table with a refit at startup, no code change.
- `CostMeter.record(UsageLine)` (sync) = `Outbox.put(OutboxRecord(kind="usage_line", id=line.id, …))` plus `aghoo_cost_usd_total{tenant, line}`.
- Who records which line, at what granularity, with which deterministic id (`h(x) = sha256(x).hexdigest()[:26]`), so outbox replays are idempotent and Σ lines equals the response usage exactly (each line sums per-call usage and per-call rounded cost):

  | Recorder (when) | Lines | One line per | `id` |
  |---|---|---|---|
  | decision engine (`DecisionRun.finalize`) | `decision` (purpose decision/router), `verification`, `waste` (its cancelled calls) | (decision, line, Bedrock id) | `h(f"{decision_id}:{line}:{model_id}")` |
  | decision engine, L2 hit | `cache_savings` (the fill-time usage and cost; `model_id`, `region` NULL) | decision | `h(f"{decision_id}:cache_savings")` |
  | orchestrator (turn completion, from `ProfileRun.result()`) | `generation` (the committed attempt), `waste` (every other attempt) | attempt | `h(f"{attempt_id}:{line}")` |
  | labeling job | `labeling` | (job attempt, question, labeler) | `h(f"labeling:{job_id}:{attempt}:{question_name}:{model_id}")` |

- `DecisionResult.usage`, `TurnObject.usage` and `turn.completed.usage` = Σ of their lines **except** `cache_savings`.
- `GET /v1/usage` (IMPL-040) → `UsageRepo.report`, under `uow(principal)` (RLS gives the project or tenant scope, spec-09 §3):

  ```sql
  WITH r AS (
    SELECT (<key>) COLLATE "C" AS key, line, sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens,
           sum(cached_input_tokens) AS cached_input_tokens, sum(cache_write_input_tokens) AS cache_write_input_tokens,
           sum(reasoning_tokens) AS reasoning_tokens, sum(cost_usd) AS cost_usd,
           bool_or(cost_estimated) AS cost_estimated, bool_or(tokens_estimated) AS tokens_estimated
      FROM noema.usage_line WHERE day BETWEEN :from AND :to GROUP BY 1, 2)
  SELECT * FROM r WHERE <after> ORDER BY key NULLS LAST, line LIMIT :limit + 1
  -- <key>: project_id | session_id | model_id | to_char(day, 'YYYY-MM-DD') | line   (group_by)
  -- <after>: true on the first page; for cursor (k, l): (key IS NOT NULL AND (key, line) > (:k, :l)) OR key IS NULL
  --          when :k is not null, and (key IS NULL AND line > :l) when :k is null
  ```
  - the cursor is base64url(orjson([key, line])) of the last returned row; the extra row decides `has_more`;
  - `cache_savings` rows carry the avoided cost; `cost_usd` is serialized as a JSON number with 6 decimals.

## 3. Write-behind outbox (ops-ledger; IMPL-022)

```text
Outbox.put(record)   sync: append to an in-process deque (bounded 100k; at the bound, spill straight to Redis; if Redis is also
                     down, drop the oldest + aghoo_outbox_dropped_total + an ERROR log that pages)
batch writer         every 200 ms or 500 records: SystemCalls.outbox_apply(records); records it rejects, or the whole batch when
                     Postgres is down -> XADD RedisKeys.outbox(crc32(tenant_id) % 8) (orjson record); aghoo_outbox_backlog{sink="redis"}
drainer              every 1 s, one consumer per pod in group "drain" on all 8 shards: XREADGROUP, then XAUTOCLAIM entries idle > 30 s
                     (a dead pod's); outbox_apply; applied -> XACK; failed -> left pending; delivery count ≥ 10 -> outbox_dead + XACK
both sinks down      in-process deque only (bounded); aghoo_outbox_backlog{sink="memory"}; alert
after apply          turn_finalize -> DEL RedisKeys.session_pending(session_id); also after outbox_dead of a turn_finalize record
                     (dead-lettered: alert; otherwise the session would stay busy until the 24 h TTL)
graceful shutdown    flush(timeout 10 s)
```

`ops.outbox_apply` per kind; every insert is `ON CONFLICT (primary key) DO NOTHING`, so replays are no-ops:

| Kind | `payload` | Apply |
|---|---|---|
| `usage_line` | the `noema.usage_line` row | INSERT |
| `audit_event` | the `noema.audit_event` row | INSERT |
| `decision_row` | the `noema.decision` row (a `/v1/decisions` insert that failed) | INSERT |
| `turn_finalize` | `{lease: {session_id, turn_id, last_seq} \| null, turn, attempts[], decisions[], turn_files[], sticky_spec}` | INSERT the rows; with a lease: `UPDATE noema.session SET last_seq = :last_seq + 1, sticky_spec, active_turn_id = NULL, lease_expires_at = NULL WHERE id = :sid AND last_seq = :last_seq`. 0 rows and the turn row already existed → applied (a replay); 0 rows otherwise → error `seq_conflict` (retried, then dead-lettered with an alert) |
| `labeling_offer` | `{decision_id, turn_id, question_names[], sample_reason}` | INSERT `jobs.job(kind 'labeling.item', priority 200, dedupe_key = decision_id)` |

The orchestrator, when it puts a `turn_finalize` record, first sets `SETEX RedisKeys.session_pending(sid) 86400 <turn_id>`; while the key exists, the caller of `acquire` treats the session as busy, even after the lease expired (spec-02 §7.1, spec-04 §2), so no other turn can take the next `seq`. The drainer DELs it after the apply or the dead-lettering of that record.

## 4. Jobs, periodic jobs, notify bridge (ops-ledger; spec-03 §2 `jobs.job`)

- `JobQueue.enqueue` = INSERT under `tenant_uow(tenant)` plus `pg_notify('jobs', kind)` (spec-02 §7).
- Workers hold one dedicated connection that `LISTEN jobs`; on a notification for one of their kinds, and every 5 s regardless, they claim through `jobs.claim(kinds, n = free slots, worker = "<pod>:<pid>", lock_s = 60)`. The lock is extended every 20 s (`jobs.extend`, fenced by `worker`); the handler runs under `tenant_uow(job.tenant_id)` or, for `'__platform__'` jobs, `system_uow` plus per-tenant `tenant_uow`.
- Retries (`jobs.finish`): backoff `30 s × 2^(attempt − 1)` (30, 60, 120, 240 s), `dead` at the job's `max_attempts` (default 5; `media.process` 2) or on `PermanentJobError`; `SystemCalls.finish_job` returns the new state, and `dead` logs an ERROR (alert rule on the log-based count).
- Kinds in Phase 1 (lower priority value = claimed first):

  | Kind | Scope | Payload | Priority | Enqueued by | Process |
  |---|---|---|---|---|---|
  | `media.process` | tenant | `{file_id}`, `dedupe_key = file_id` | 10 inline images, 100 uploads | FileService | media-worker |
  | `file.purge` | tenant | `{file_id}`, `dedupe_key = file_id` (S3 objects, then the rows: `FileService.purge`) | 300 | `FileService.delete` | media-worker |
  | `session.delete` | tenant | `{session_id}`, `dedupe_key = session_id` | 100 | identity (`DELETE /v1/sessions/{id}`) | jobs |
  | `labeling.item` | tenant | `{decision_id, turn_id, question_names, sample_reason}`, `dedupe_key = decision_id` | 200 | `ops.outbox_apply` (`labeling_offer`) | jobs |
  | `labeling.run` | tenant | `{run_id}` (fit + acceptance gate of one run, ADR-003), `dedupe_key = run_id` | 200 | labeling | jobs |
  | `labeling.drift` | `'__platform__'` | `{}`; weekly | 300 | the periodic scheduler | jobs |
  | `retention.sweep` | `'__platform__'` | `{}`; hourly | 300 | the periodic scheduler | jobs |

  `labeling.*` handlers call Bedrock with the job tenant's STS credentials (D2); `labeling.drift` and `retention.sweep` iterate tenants themselves.
- **Periodic scheduler** (every `jobs` process, every 60 s): `jobs.ensure_periodic('retention.sweep', '1 hour')` and `jobs.ensure_periodic('labeling.drift', '7 days')`. The `job_dedupe` index keeps at most one queued or running instance; a finished one is replaced by the next call at the next period boundary, so several `jobs` processes are safe.
- **Notify bridge** (every `jobs` process): one connection `LISTEN noema_events`; each notification `{channel, payload}` from the `admin.*` and labeling definer functions is published unchanged to `RedisKeys.config_changed()`, `calibrators_changed()` or `keys_revoked()`. Several bridges publish duplicates, which receivers tolerate (each handler is idempotent). Pods also poll (config and calibrators every 5 s; key LRU entries expire after 60 s), so a bridge or Redis outage delays changes but never loses them.

## 5. Idempotency store (ops-ledger; IMPL-077)

Postgres `idempotency_record` (through `IdempotencyRepo`, under `uow(principal)`) plus Redis notifications. `scope_hash = sha256(f"{project_id}\n{method}\n{route_template}")`; `fingerprint = sha256(canonical body (sorted keys, no whitespace) + "\n" + Aghoo-Version)`, stored and compared bound to the principal as `sha256(fingerprint + "\n" + principal_id)` (another principal's reuse → Conflict; IMPL-077 amendment, R7); `owner = "<pod>:<request_id>"`.

```text
begin(…, resource_id): INSERT (state in_flight, lease_expires_at = now() + 30 s, expires_at = now() + ttl_s, resource_id)
                       ON CONFLICT (tenant_id, project_id, scope_hash, key) DO NOTHING
  inserted                                         -> New(token)
  existing, expires_at < now()                     -> take over as New  (an expired record counts as absent)
  existing, fingerprint differs                    -> Conflict          (409 idempotency_key_reused)
  existing completed                               -> Replay(stored)    (x-aghoo-idempotent-replay: true; a response_body_key body
                                                      is read with GetObject; a GET failure -> 500 internal_error, retryable,
                                                      the record stays completed; IMPL-085)
  existing not_stored                              -> take over as New  (pre-call failure; re-executes)
  existing in_flight, lease alive                  -> InFlight(token, resource_id)
  existing in_flight, lease expired                -> take over as New  (owner died)
take over = UPDATE … SET state = 'in_flight', owner, fingerprint, resource_id, lease_expires_at, expires_at, created_at = now(),
            response_body = NULL, response_body_key = NULL
            WHERE id = :id AND <the condition read above still holds>; 0 rows -> re-read and re-classify (a racing taker won)
heartbeat: every 10 s, lease_expires_at = now() + 30 s WHERE owner = :me; False -> the caller logs and carries on (it can't stop
           a running turn; a duplicate may take over and re-execute)
await_result: SUBSCRIBE idem_done(scope_hash, key) + a 250 ms poll until completed, the deadline (504), or not_stored / a lapsed
              lease (500 internal_error, retryable)
complete(token, resp): resp None -> state not_stored (no call dispatched, or a JSON response whose content the request may not
                       keep: store false or store_content != full; R7); else completed with the response WHERE owner = :me; then
                       PUBLISH idem_done.
                       body ≤ IDEM_INLINE_MAX_BYTES (1 MiB) -> response_body (bytea); larger -> first PutObject
                       s3://noema-<env>/<tenant_id>/idempotency/<record_id> (SSE-KMS, tenant key), then the UPDATE with
                       response_body_key and response_body NULL (IMPL-085). Size never gives not_stored. PutObject failing twice
                       -> not_stored + aghoo_idempotency_store_errors_total (the only dispatched-call re-execution path)
streamed turns: complete stores {status 200, sse_turn_id = turn_id, body NULL} at turn completion; an InFlight duplicate attaches
                with TurnEventLog.subscribe or attach_remote(resource_id); a Replay attaches to the mirror, or 410
                resume_window_expired (details.turn_id) once the mirror has expired; a keyed stream counts as dispatched once its turn
                started, so a retry after a dropped connection attaches instead of running a second turn (R7)
```

Expiry: `platform.idempotency.ttl_s` (24 h), deleted by `retention.sweep` (spec-03 §5) together with the record's object (`DeleteObject <tenant_id>/idempotency/<record_id>` for every swept record, a no-op when absent), so a large body has exactly the record's TTL (IMPL-085). S3 down: a large-body `complete` → `not_stored` (above); a large-body replay → 500 (retryable).

Postgres down: `begin` raises 500 `internal_error` (retryable), so a request that carries `Idempotency-Key` fails fast instead of risking a double execution; requests without the header keep the IMPL-022 behavior (`store: false` turns and `/v1/decisions` continue). Redis down: `idem_done` is lost, and `await_result` falls back to the 250 ms poll.

## 6. Retention sweeper, reapers, watchdog (ops-ledger)

- **Retention** (`retention.sweep`, hourly): page `SystemCalls.retention_page(1000, after=<last candidate>)` (`ops.retention_page`, keyset on `(expires_at, kind, id, tenant_id)`, so rows that keep failing never hold a page's head) until empty → per row, delete under `tenant_uow(tenant)` with the repo `delete` of its kind (spec-03 §5) → S3 deletes for files and for swept idempotency records (IMPL-085) → one `audit_event` per tenant and run (`action = retention.sweep`, counts only). L2 entries expire on their own (TTL). Legal hold is Phase 2.
- **Reaper** (every 30 s, every `jobs` process; concurrent runs are harmless): `ops.reap_stuck('2 minutes')` requeues jobs whose lock lapsed and deletes old finished jobs. Session leases need no reaper (an expired lease is free, spec-04 §2; a pending outbox finalize keeps the session busy through `session_pending`).
- **Attempt watchdog** (in-process, generation): when a turn task exits, every attempt not in a terminal state is marked `cancelled(deadline)` with estimated usage, and `aghoo_stuck_attempts_total` is incremented (the soak gate: no stuck attempts).

## 7. Health, readiness, metrics, shutdown (ops-ledger)

| Endpoint | Behavior |
|---|---|
| `GET /healthz` | 200 while the event loop answers |
| `GET /readyz` | 200 when the config snapshot is loaded and the pod is not draining (`runtime.draining`), else 503; no body (unauthenticated, so it discloses no dependency states; a degraded dependency doesn't make the pod unready, 06 §7 availability) |
| `GET /metrics` | Prometheus text on the internal port only (`NOEMA_METRICS_PORT`); every 08 §7 instrument plus the IMPL-006 additions |

Graceful shutdown on SIGTERM (api; `composition.start` chains the handler, `composition.stop` runs the steps, spec-01 §3):
1. `/readyz` → 503 at once (`runtime.draining`); the drain hooks (`ProcessRuntime.on_drain`) start with the 30 s grace;
2. stop accepting new requests: uvicorn's own SIGTERM handler runs `NOEMA_DRAIN_DELAY_S` later (run uvicorn with `--timeout-graceful-shutdown` above the grace; compose stop grace 45 s);
3. running turns continue up to 30 s, then are cancelled with `error{all_routes_unavailable, retryable: true}` and get up to 5 s to finalize; SSE writers end after `turn.completed`;
4. `Outbox.flush(10 s)`, after every turn has finalized (their usage lines and finalize records are in the outbox);
5. stop the background tasks of spec-01 §3; close the Redis, Postgres and HTTP pools;
6. exit.

The `jobs` and `media-worker` processes stop claiming, give running handlers 10 s (spec-02 §7 `Worker.run`), flush the outbox and exit; unfinished jobs return through the reaper.

**Process sizing and overload (IMPL-087; a note on 08 §9, 08:L488).**
- **Sizing.** A 2-vCPU api pod runs **2 api processes** (`WEB_CONCURRENCY=2`, uvicorn workers on one socket). Each process is its own pod for leases and owners and has its own metrics port (spec-05 §5). The pods are sized from the measured L1 knee (owner decision 2026-10-12): at most 125 streams per pod on the shipped Wave F image in the container, planned at 100 (08 §9, `reports/perf/sizing.md` §5, IMPL-089; the E3 image: 75, planned at 60). Wave E2's "~200 streams per pod = 2 × 100" is superseded.
- **Per-process cap.** `NOEMA_MAX_TURNS_PER_PROCESS` (default 120) bounds the running turns plus in-flight createDecision and facade requests of one process.
  - Above it: **429 `queue_timeout`** with `retry-after: 1`, before any Postgres, Redis or model work; counted in `aghoo_turns_shed_total`.
  - The cap is checked twice (Wave E3, IMPL-087 amendment). The first check is at the edge, before the middleware stack (`noema.api.shed`). It covers an unkeyed `POST` whose key id the in-process key cache holds; the tenant is not verified there. A key id the cache does not hold always gets the full ingress, so its 401 keeps precedence. Over the cap, a wrong secret for a cached key id is 429, and the gate's 400/413/415 are not reached. Edge refusals get no access line, only one `edge shed` summary line per second at most. The second check, the authoritative one, is in the route after authentication and the request gate.
  - The slots are tenant-fair: the max-min fair share of the cap among the tenants holding slots. A tenant below its share is admitted up to cap + cap ÷ 10. A tenant whose turn timed out in the admission queue is saturated for 2 s and holds at most cap ÷ 10 waiting turns per process. Those turns do not count against other tenants.
  - A keyed request is shed only at step 7, so its duplicates attach or replay.
- **GC.** Outside test, startup ends with `gc.collect(); gc.freeze()` and `gc.set_threshold(700, 10, 100)` (Wave E3; Wave E2 used `(700, 10, 40)`), unless the thresholds were already changed.

## 8. Tests (owners; `tests/unit/ops/`, `tests/integration/ops/`)

| Id | What | Pass |
|---|---|---|
| T-WG-1…5 | the §1.1 vectors (T-WG-5: hierarchical, 1 + 3 calls per member) | exact counts per alias and kind |
| T-GCRA-1 | the Lua script against a Python reference model (hypothesis: random debits, refunds, clock steps) | identical allow/deny, `retry_after_s`, `remaining` |
| T-GCRA-2 | all-or-nothing: one failing bucket debits nothing; stream slots expire with the lease | asserted on Redis state |
| T-ADM-1 | WFQ: 2 tenants, one flooding | the other's queue wait ≤ 10% worse (spec-10 load row) |
| T-ADM-2 | Redis down → local buckets at limit ÷ `NOEMA_EXPECTED_PODS` | admits, then 429 at the local limit |
| T-ADM-3 | reservation ≥ issued, on the load run (spec-10) | `aghoo_reservation_overrun_total` = 0 |
| T-COST-1 | price lookup precedence (`price_ref`, id, region, `effective_from`), the formula against hand-computed Decimal vectors, TBD → estimated | exact |
| T-COST-2 | line granularity and deterministic ids (§2 table); Σ lines = `DecisionResult.usage` / `turn.completed.usage` | exact equality |
| T-USAGE-1 | the report for every `group_by`, null keys last, keyset pages without gaps or repeats, developer vs tenant_admin scope | asserted |
| T-OBX-1 | Postgres down → Redis → drained exactly once (idempotent replays); both down → memory; delivery count 10 → dead letter | asserted |
| T-OBX-2 | `turn_finalize` with `session_pending`: the next turn is busy until the drain; a replayed record is a no-op | asserted |
| T-IDEM-1 | 2 processes: attach JSON, attach SSE, replay, conflict, owner-death takeover, not_stored re-execution, racing takeovers | one winner |
| T-IDEM-2 | large bodies on minio (IMPL-085): a 3 MiB `/v1/decisions` result replays byte-identical from `<tenant>/idempotency/<id>` with no model call and no new usage line; a 1 MiB body stays inline; PutObject failing twice → `not_stored` + metric; GetObject failure → 500 and the record stays `completed`; sweep and take-over remove or overwrite the object; tenant B can't read tenant A's key | exact |
| T-JOB-1 | SKIP LOCKED with 4 workers: no double processing; lease extension; stale-worker finish ignored; dead after `max_attempts` | asserted |
| T-JOB-2 | `ensure_periodic` from 3 processes | one instance per period |
| T-RET-1 | rows past `expires_at` are deleted per tenant only (RLS); label excerpts nulled with their decision | asserted |
