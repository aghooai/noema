# noema.ops.admission (owner: ops-admission; Wave C1, C2a adaptation)

Spec: spec-11 §1 (work graph, admission), spec-02 §8 (`WorkGraphPlanner`, `Admission`, `Reservation`,
`RateLimiter`), spec-05 §1.1-§1.2 (limits), IMPL-016, -027, -078, -084. `TokenEstimator` is **not** here
(ops-ledger, spec-11 header).

## Purpose
- **Plan** the worst case of one request (`WorkGraph`): every scheduled decision call (router questions x members,
  backups, hierarchical 1 + 3, escalation stages, verification) and every generation attempt (4 waves x the widest
  candidate profile), with reserved tokens (input estimate + 600 per decision call + `max_tokens`).
- **Admit** it atomically at ingress: GCRA RPM/TPM buckets (key, project, per-model quota) and stream / bulkhead
  slots in one Redis Lua call, all-or-nothing; 429 at once on rate exhaustion; a per-pod weighted fair queue for
  slot waits; a per-pod local fallback when Redis is down.
- **Track** the reservation: sync `consume`, `reconcile` refunds, `release` frees the slots; `headers()`.
- **RateLimiter**: the bare GCRA bucket of the per-IP auth-failure limit (spec-09 §2).

## Public surface (`from noema.ops.admission import ...`)
| Name | Kind | Notes |
|---|---|---|
| `DefaultWorkGraphPlanner()` | `WorkGraphPlanner` | no constructor args; sync, pure; `routing.default_profile` is always a generation candidate |
| `GcraAdmission(clock, config, keys, redis, *, env, instruments=None, expected_pods=1, redis_timeout_s=0.05, probe_interval_s=1.0, poll_s=0.1)` | `Admission` | `redis=None`: in-process buckets only (no division by pods); `.degraded` = Redis-down fallback active |
| `GcraReservation` | `Reservation` | returned by `reserve`; also `.graph` (the reserved `WorkGraph`) |
| `GcraRateLimiter(clock, keys, redis, *, expected_pods=1, redis_timeout_s=0.05, probe_interval_s=1.0)` | `RateLimiter` | `redis=None`: in-process |
| `AdmissionRejected(NoemaError)` | error | the 429 of `reserve`; `.headers` = x-ratelimit-* of the buckets as they stand (nothing debited) |

Internal modules: `planner.py`, `plan.py` (buckets/slots of one reservation), `gcra.py` (arithmetic + `LocalStore`),
`lua.py` (`RESERVE`, `REFUND`), `stores.py` (`RedisStore`, `FailoverStore`), `fair_queue.py`, `admission.py`,
`rate_limiter.py`.

## Wiring in `composition.py` (C2 integrator)
```python
from noema.ops.admission import DefaultWorkGraphPlanner, GcraAdmission, GcraRateLimiter

planner = DefaultWorkGraphPlanner()
admission = GcraAdmission(
    clock, config, keys, redis,                      # Services.clock, .config, .keys, .redis (bytes client)
    env=settings.env,                                # "test" -> a reservation overrun raises AssertionError
    instruments=telemetry.instruments,
    expected_pods=settings.expected_pods,            # NOEMA_EXPECTED_PODS: the Redis-down divisor
)
rate_limiter = GcraRateLimiter(clock, keys, redis, expected_pods=settings.expected_pods)  # -> the Authenticator
```
- One `GcraAdmission` per process (it owns the per-pod fair queue and the fallback state); no background task,
  no `start`/`stop`, nothing to drain (slots of a killed pod expire with their lease).
- The Lua scripts are registered on construction (`register_script`; EVALSHA with a NOSCRIPT reload).

## Caller contract
- `reserve(principal, graph, queue_timeout_s=snapshot.platform.limits.admission_queue_timeout_ms / 1000,
  deadline=ctx.deadline)`; pass the deadline whenever `graph.streams > 0` (slot lease = deadline + 30 s; without
  it the lease is 600 s + 30 s).
- Then `try: ... finally: await res.release()`; call `await res.reconcile(usage_by_alias)` when the usage is
  known (alias -> `UsageRecord`). Both are idempotent and shielded (1 s); a cancellation that lands while
  `reserve` completes rolls the reservation back (nothing stays debited).
- Allowed orders: `reconcile` then `release` (calls and tokens refunded by reconcile, release frees the slots);
  `release` then `reconcile` (release frees the slots and refunds the unused calls, the later reconcile refunds
  only the unused tokens); `release` alone (unused calls back, tokens stay debited). The unused calls are refunded
  exactly once, by whichever comes first.
- Engines call `res.consume(kind, alias)` before each model call (sync, a dict update).
- Turns: `SseResponse(headers=res.headers())`; on a later HTTP error `deps.add_error_headers(request,
  res.headers())`; on a 429 from `reserve`, `err.headers` (on `AdmissionRejected`) may be added the same way.
- Labeling jobs reserve a hand-built `WorkGraph` whose calls are of kind `labeling` with
  `system_principal(tenant, purpose="labeling", ...)`.

## Config keys read (`config.current()` at each reserve)
`platform.limits.{rpm_per_key, tpm_per_key, rpm_per_project, tpm_per_project, concurrent_streams_per_project}`,
`tenants.<t>.limits.{same five, model_quotas.<alias>.{rpm, tpm}}` (the tenant value wins). Planner (the pinned
snapshot passed in): `decision_profiles.*` (members, templates, deadlines, backups, escalation),
`templates.*.{elicitation, max_choices}`, `models.*.{controls.reasoning, reasoning_reserve_tokens,
max_output_tokens_cap}`, `generation_profiles.*`, `routing.{default_profile, bindings[].then.max_output_tokens}`.
`admission_queue_timeout_ms` is read by the caller (the Protocol takes `queue_timeout_s`).

## Behavior (spec-11 §1.2)
- **Buckets** (`RedisKeys.gcra(name)`): `key:{key_id}:rpm|tpm` (principals with a key), `prj:{tenant}:{project}:rpm|tpm`
  (principals with a project), `mdl:{tenant}:{alias}:rpm|tpm` (aliases of the graph with a model quota). RPM debits
  calls (`total_calls`, or the alias's calls over every kind), TPM debits `tokens_in + tokens_out` (or
  `tokens_by_model[alias]`). GCRA: `T = 60 000 / limit` ms, `τ = 60 000` ms; TATs are wall-clock ms from the
  injected `Clock` (all pods share them; see Known limits for the clock-skew dependency).
- **429s**: `rpm_exceeded` if any RPM bucket fails, else `tpm_exceeded`; `retry_after_s` = the largest
  `ceil((tat' - now - τ) / 1000)` of the failing buckets (60 when n > limit). Nothing is debited.
- **Slots** (`RedisKeys.streams` ZSETs, member = `<request_id>:<random>`, score = lease expiry ms): the project's
  streams (cap `concurrent_streams_per_project`, one member per stream) and one bulkhead place per quota'd alias
  (`streams(tenant, "mdl:" + alias)`, cap `ceil(rpm / 6)`). Expired members are dropped by every reserve.
- **Fair queue** (per pod): a full slot set queues the request; round robin over tenants (DRR with weight 1 and
  cost 1 per attempt), FIFO within a tenant per slot set; retried on every local release and every 100 ms; the
  timeout is `queue_timeout`, retry-after 1. A newcomer whose slot sets have queued waiters queues behind them,
  but its buckets are checked first without debiting (check-only mode of the script), so rate exhaustion is still
  `rpm_exceeded` / `tpm_exceeded` at once with the real retry-after.
- **Labeling** graphs (any `labeling` call): only the buckets the principal has (system principals: the `mdl:`
  ones), each admitted only while 20% stays free (`LABELING_HEADROOM`). The same headroom applies to every slot
  set a labeling reservation takes a place in: at most `floor(0.8 · cap)` live places (`Slot.limit`; so a
  1-place bulkhead, `rpm <= 6`, is never taken by labeling). Labeling never waits in the fair queue: short of
  bucket or slot headroom, or behind queued waiters, it is `queue_timeout` at once (the job retries with its
  backoff), so best-effort labeling can neither fill a bulkhead nor block user waiters.
- **consume / reconcile / release**: `consume` above the reserved `(alias, kind)` count logs ERROR and counts
  `aghoo_reservation_overrun_total{kind}` (AssertionError when `env == "test"`); the call proceeds. `reconcile`
  refunds `reserved - consumed` calls (unless `release` already did) and `max(0, reserved - (input + output))`
  tokens per bucket; `release` frees the slots and, without a prior reconcile, refunds only unconsumed calls.
- **Redis down** (a reserve/peek script or connection error, or a call slower than 50 ms): every bucket and slot
  cap becomes `limit // NOEMA_EXPECTED_PODS` (at least 1) in process memory; Redis is probed again 1 s later (by
  the next reserve); on the success of such a probe (a call that started while degraded: a call already in flight
  when another one failed does not end the outage, Wave E) new reservations use Redis again and the local state is
  set aside, never merged.
  Hysteresis: if Redis fails again within `LOCAL_KEEP_S` (60 s, one admission τ) the set-aside state is reused,
  so a flapping Redis (or event-loop stalls over 50 ms) does not hand the pod a fresh local burst per flap; a
  later outage starts from a fresh share. `GcraRateLimiter` uses the same fallback and never raises.
- **A failed writing reserve is undone on Redis** (Wave E): it may have run (its reply came after the 50 ms bound,
  e.g. behind an event-loop stall, or the connection broke after the send). Every writing `RESERVE` carries a fresh
  marker key `gcra:debit:<16 hex>`; the script refuses to run if the marker exists and sets it to `1` (60 s) when
  it writes. On a failure `RedisStore` sends one bounded `REFUND` with that marker: it refunds the buckets only if
  the marker is `1`, removes the slot members (ZREM), and sets the marker to `x`, so the undo is exactly-once and a
  reserve that reaches Valkey after it writes nothing. The reservation then goes to the local share as before. If
  the undo fails too, its slot releases are kept as below (counted in `aghoo_admission_refund_failures_total`)
  and its bucket units refill within a minute. Before this, the Redis debit stayed: phantom stream-slot and
  bulkhead members until `deadline + 30 s`, and the units charged twice.
- **Refunds and releases** always go to the store that debited them: a Redis debit is refunded on Redis even
  while reserves use the fallback (one call bounded by 50 ms, inside release's 1 s shield, off the request path);
  a failing refund never marks Redis down. If it fails (or is cancelled), its bucket refunds are lost (a call
  that timed out may have run, so a retry could refund twice; the buckets refill within a minute) and its slot
  releases are kept (at most `MAX_PENDING_SLOTS` = 4096, oldest dropped) and carried by the next Redis refund (up
  to `RETRY_BATCH` = 256 per call; ZREM is idempotent). `FailoverStore.pending_releases` is the kept count.

## Metrics, spans, logs
- `aghoo_reservation_overrun_total{kind}` (counter), `aghoo_queue_depth` (gauge, this pod's queue).
- `aghoo_admission_rejections_total{code}` (counter): every `AdmissionRejected` of `reserve` (`rpm_exceeded`,
  `tpm_exceeded`, `queue_timeout`, labeling included); a cancelled `reserve` is not counted.
- `aghoo_admission_degraded` (gauge 0/1): 1 from the first failed Redis call of an outage, 0 when a probe succeeds;
  set on the transitions only, and 0 at construction (also for `redis=None`).
- `aghoo_admission_refund_failures_total` (counter): Redis refunds that failed or were cancelled (the release shield
  ran out); their bucket refunds are lost and their slot releases kept.
- `aghoo_admission_pending_releases` (gauge): `FailoverStore.pending_releases`, set whenever the kept list changes
  (a failed refund, or a refund that carried kept releases); 0 at construction.
- All four come from the `instruments` passed to `GcraAdmission`; `GcraRateLimiter` (the per-IP auth bucket) emits
  none (its own fallback would clobber the label-free gauges).
- No spans (no admission `SpanName` exists).
- Logger `noema.ops.admission`: ERROR per overrun, WARNING when Redis is lost, INFO when it returns, WARNING
  when a Redis refund fails with no releases kept yet (the start of a failing streak, not per failure).

## Error codes raised
`rpm_exceeded`, `tpm_exceeded`, `queue_timeout` (429, `rate_limit_error`, always with `retry_after_s`), as
`AdmissionRejected`. `GcraRateLimiter` raises only `ValueError` for invalid parameters.

## Tests
- Unit (`tests/unit/ops_admission/`, no network): `test_planner.py` (T-WG-1…5 with exact counts per alias and kind,
  token formulas, escalation filter, race width, IMPL-084 out(m)), `test_plan.py`, `test_gcra.py`,
  `test_fair_queue.py` (T-ADM-1 fairness, FIFO, poll tick, timeouts, cancellation, rollback),
  `test_admission.py` (headers, codes, retry-after, reconcile/release math incl. the T-WG-5 refund and
  release-then-reconcile, labeling bucket and bulkhead headroom, labeling never queues, rate exhaustion behind
  queued waiters, bulkheads, leases, cancellation, overrun, rejections counted by code), `test_failover_store.py`
  (refunds of Redis debits while degraded, kept slot releases and their bounds, cancelled refunds, flapping-Redis
  hysteresis and the keep window, the degraded / refund-failure / pending-release metrics), `test_reservation_bound.py` (T-ADM-3 module part: every IMPL-016 attempt walk fits the reservation,
  overrun counter 0).
- Integration (`tests/integration/ops_admission/`, compose Valkey + host toxiproxy): `test_lua_gcra.py` (T-GCRA-1
  hypothesis vs an independent reference model with its own tolerance, over per-minute, labeling-headroom and
  RateLimiter buckets, plus check-only mode and the local store on the same steps; T-GCRA-2 all-or-nothing,
  check-only writes nothing, leases, on the Redis state), `test_redis_admission.py` (atomic reservations and
  stream slots from two pods, cross-pod release, shared refunds), `test_redis_rollback.py` (cancellation during
  the direct attempt and during a pumped attempt rolls back on Redis; a transient failure leaks no Redis slots or
  calls; the script is gated, so no timing), `test_failover.py` (T-ADM-2: down -> limit ÷ pods then 429 via
  toxiproxy and, without toxiproxy, on a closed port, with the degraded gauge and the rejection counter; recovery;
  slow Redis; slot releases lost in an outage are retried after it, with the refund-failure counter and the
  pending-release gauge; rate limiter). Tests that need toxiproxy skip when it is not running; the closed-port case
  always runs.
- Conformance (`tests/integration/ops_admission/conformance_impls.py`): `work_graph/default`,
  `admission/gcra-local`, `admission/gcra-redis` (integration), `rate_limiter/gcra-local`,
  `rate_limiter/gcra-redis` (integration).

## Known limits
- Multi-key scripts need a non-cluster Redis/Valkey (or one hash slot); `RedisKeys` adds no hash tags.
- A script that ran but answered after the 50 ms bound is treated as failed: its debit stays in Redis until the
  bucket refills, and its slots until their lease ends (fail-safe, never over-admits on Redis).
- While Redis is down each pod admits its own share; when Redis returns, live local reservations are not
  migrated (spec: dropped, not merged), so the first minute after recovery can admit up to one extra share. With
  the hysteresis, a Redis flapping within 60 s adds at most one share per pod per minute in total (the
  continuous-outage bound), not one per flap. Interpretation of spec-11 "local state is dropped, not merged":
  the state is unused while Redis answers and never merged into Redis; it is only reused if Redis fails again
  within 60 s (request 5, confirmed by ruling R4; spec-11 §1.2 now says so).
- A failed Redis refund loses its bucket refunds (under-admission for up to a minute, never over-admission); a
  failed slot release is retried with later refunds, so it waits for traffic (or its lease) and the kept list
  is bounded (4096 releases).
- **Clock skew.** TATs and slot leases are the caller's wall clock, sent to the script (not Redis `TIME`), so the
  script stays deterministic and testable against the reference model and a fake clock. Every pod therefore
  needs NTP-synced clocks (Amazon Time Sync keeps skew in the ms range). A pod whose clock runs ahead by Δ gets
  about Δ/T extra units on each bucket; and because each reserve purges slot members whose lease is <= its own
  `now`, a skew Δ > `LEASE_GRACE_MS` (30 s) evicts other pods' live slot members with less than Δ - 30 s left
  before their deadline, which can exceed `concurrent_streams_per_project` and the bulkheads. Alert on clock
  offset well below 30 s.
- Labeling never takes a 1-place bulkhead (`model_quotas[alias].rpm <= 6`), and in the Redis-down fallback a
  per-pod share of 1 place is not taken by labeling either.
- Fairness is per pod; cross-pod fairness comes only from the shared buckets. A slot freed on another pod is seen
  at the next 100 ms poll.
- The planner's `EFFORT_RESERVE` and `DECISION_PROMPT_OVERHEAD` (spec-06 §5.5 / spec-11 §1.1) are imported from
  `noema.config.models` since C2b (request 3 closed; the local copies are gone); `test_planner.py` still pins the
  values through the formulas.
- T-ADM-3's load-run half (overrun = 0 over the C1 load) and the C5 fairness load row belong to QA (Wave E).

## C1 review fixes (independent review, 7 findings)
1. Fixed: `release` then `reconcile` refunded the unused calls twice; now once, by whichever comes first.
2. Fixed: refunds of Redis debits were dropped for 1 s after any slow call; now always sent to Redis, a failing
   refund never marks Redis down, and failed slot releases are kept and retried.
3. Fixed: a flapping Redis gave a fresh local burst per flap; the set-aside local state is reused within 60 s.
4. Fixed: a request behind queued waiters skipped its rate check; buckets are now checked first (check-only).
5. Fixed: labeling could fill a bulkhead; labeling keeps 20% of slot places free and never queues.
6. Fixed: added the rollback, degraded-refund, release-then-reconcile and flapping tests, an independent
   T-GCRA-1 tolerance with headroom and RateLimiter variants, and a closed-port T-ADM-2 case.
7. Documented (Known limits, clock skew). Not changed to Redis `TIME`: server time would make the scripts
   untestable against the reference model and the fake clock (T-GCRA-1/2, T-ADM-2), and mixing time bases
   (fallback and headers on the pod clock) adds its own skew; the eviction needs a skew over 30 s, which NTP
   prevents. A purge clamp at `now - 30 s` was also not added: it only moves the threshold and doubles how long a
   dead pod's slots stay.
- Constructors, the public surface and the composition wiring are unchanged. The Lua `RESERVE` script gained
  one ARGV (`check`), so its SHA changed (it is registered on construction; nothing to migrate).

## C2a adaptation (2026-10-10)
- Request 1 (module half): the four admission instruments are emitted (see Metrics). `FailoverStore` gained a
  keyword `instruments=None` (internal; `GcraAdmission` passes its own). No public signature changed.
- Request 3: open after C2a; closed in C2b (both constants live in `noema.config.models`, Known limits).
- Requests 2, 4, 5: done by the appliers; nothing to change here (the real `GcraAdmission` already emitted integer
  reset headers, which the tightened admission conformance case now checks).
- typecheck-tests: the 3 mypy errors in `test_failover_store.py` (an identity check against the scripted primary
  and a property narrowed across reserves) are fixed with a cast and a `degraded(store)` read helper, as in the
  integration tests.
- Other C2a interface changes (FakeClock blocking rules, `CostMeter.price(snapshot=)`, `RetryBudget.note_request`,
  storage, media, labeling) do not touch this module; the suites pass unchanged on the new FakeClock.
