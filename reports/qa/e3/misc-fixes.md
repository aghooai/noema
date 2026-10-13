# Wave E3 misc fixes: R8, F12 per upstream, R4 metrics, bounded Redis pool, estimator override, pub/sub heartbeat, M2, rulings

Role: misc-fixes (Wave E3), 2026-10-12. Edits in this role's paths, plus the tests that R8 or the new metrics made
stale (section 8 lists every one with its reason). No live Bedrock call, no git write command. My own stacks:
`noema-e3misc-e2e` (port bases 33700, then 23700: 33750 was taken by an ephemeral client port during a rebuild) and
`noema-e3misc-fault` (33900). Images `noema-{api,worker,media}:e3misc` from the working tree. Every stack is torn down.
Benchmarks: `reports/qa/e3/misc-fixes/bench/` (pinned with `taskset -c 5`, 1-minute loadavg 3.9-4.5 for every final
number, below the 8 limit).

## 1. Summary

| Item | Change | Before | After |
|---|---|---|---|
| R8 retry budget | `adapters/resilience/retry_budget.py`: 10% of first sends, no floor; unsent connect retry exempt and not a first send | E2 code: floor of 10 per window; 9 of the 16 budget unit tests fail on it | 16/16 pass; F12 per upstream (all ops) Sol 1.088 at `rate: 0.5`, 1.076 at 0.25 |
| F12 per failing upstream | `tests/fault/test_f12_retry_storm.py`: gate = requests to X (every operation) ÷ X's first sends, both rows | `rate: 0.5` gated on requests ÷ turns only | both rows gated per upstream; generation-only ratio recorded |
| R4 metrics | 8 metrics declared (`telemetry/registry.py`), observed in `composition.py` | no pool or datastore latency metric (C2's unmeasured part) | Postgres and Redis pool size, in use, acquire wait, command latency; +0.14 ms CPU per turn |
| Bounded Redis pool | `BlockingConnectionPool`, `NOEMA_REDIS_MAX_CONNECTIONS` (256), 0.5 s wait, fast path | unbounded `ConnectionPool` | bounded; a caller past the bound waits ≤ 0.5 s, then `ConnectionError` |
| Estimator table override | `NOEMA_TOKEN_ESTIMATOR_TABLE` (settings + composition) | no deployment wiring | a refit replaces `TOKEN_ESTIMATOR_V1` at startup; invalid file refuses startup |
| Pub/sub heartbeat | `noema/core/pubsub.py`; turn_cancel + session_released, breaker_events, calibrators_changed, config_changed | 4 listeners on `listen()` / bare `get_message`: a dead connection without FIN/RST looked idle forever | PING after 5 s quiet, resubscribe after 15 s without a reply |
| M2 | `tests/unit/turns/test_degradation_reporting.py` | no single per-reason test | one turn, 8 reasons (all with a producer) on the right event with the right `attempt_id` |
| Rulings | IMPL-DECISIONS + spec | R8, R9, E2 leftovers unrecorded; spec-09 §9 named a missing file | recorded (section 7) |
| Generation waste chars/token | not done (section 9) | - | - |

## 2. R8 in the retry budget

Rule (brief): the budget counts only re-sends the upstream may have received; it is 10% of first sends over the
sliding 60 s with no floor; a connect retry that provably never reached the upstream is exempt, limited to 1 per call.

Implementation (`SlidingRetryBudget`):
- `RetryBudgetPolicy.min_retries` defaults to 0 (kept as a test knob; nothing in `src` sets it).
- First sends = noted requests − granted re-sends − unsent sends; `try_spend` grants while `retries + 1 ≤ 0.1 × first`.
- Unsent sends: `try_spend` called while the adapter handles a transport error with `is_connect_error(exc) and not
  possibly_delivered(exc)` returns True without spending and counts the failed send as unsent (bucketed and slid like
  the other counts). The adapter's `retried` flag keeps it to one per call. The cause comes from `sys.exception()`:
  the frozen `RetryBudget.try_spend(account, model)` passes none, and `adapters/_base.py` is another role's file.
  Every other caller asks outside such an `except` block. A cleaner form is an explicit argument from `_send` (adapters
  owner; the budget would accept it without a semantic change).
- The Redis sync publishes requests net of unsent sends (same `requests,retries,wall_ms` format, so mixed versions
  interoperate), clamped at 0: an unsent mark can land one bucket after its request, so at the window's edge the
  difference could briefly be -1, which other pods' parser would reject (added after the F12 runs; unit, conformance
  and Redis tests re-run, final e2e on images with it).

Unit tests (`tests/unit/adapters/test_retry_budget.py`), run against the Wave E2 module through a `PYTHONPATH` overlay
(`git show HEAD:...retry_budget.py`) and against the new one:

| Test | E2 code | R8 code |
|---|---|---|
| `test_no_budget_without_traffic` | FAIL (`10 == 0`) | pass |
| `test_ten_percent_of_first_sends_at_every_traffic_level` [0, 9, 10, 50, 100, 205 first sends] | FAIL at 0, 9, 10, 50 (floor: 10 granted) | pass (0, 0, 1, 5, 10, 20) |
| `test_the_window_slides` | FAIL (`5 == 0`) | pass |
| `test_a_connect_retry_that_never_reached_the_upstream_is_exempt_and_not_a_first_send` | FAIL | pass |
| `test_a_connect_retry_after_a_possible_delivery_is_budgeted` | FAIL | pass |
| `test_unsent_sends_are_not_published_as_first_sends` | FAIL | pass |
| the 7 other tests (keys, sync, staleness, parsing) | pass | pass |

Whole file: E2 code 9 failed / 7 passed, R8 code 16 passed.

## 3. F12 per failing upstream (fault stack `noema-e3misc-fault`, 2 api pods x 2 workers)

The same test file on the same host: Wave E2 images (`:qae2`, the E2 final tree) vs `:e3misc` (R8). 750 turns per row
at 12.5 turns/s over 60 s.

First measurement (generation calls only, as the E2 H2 row did): `:qae2` Sol 1.207 at `rate: 0.5`, `:e3misc` Sol
1.169, both above 1.1. The cause is the base: the labelers (D4: Sol and Sonnet) make unary `converse` calls to Sol for
sampled items (155 over the stack's life, the same STS sessions as the turns), and the budget of (account, Sol) counts
them as first sends, as R8 says ("10% of first sends"), while the test counted only `converse-stream`. The gate now
counts every request to X over X's first sends: the turns with an attempt on X plus X's other requests (not re-sent
in F12: their rules have no fault). Final runs (`f12-before-qae2.json`, `f12-after.json` in `reports/qa/e3/misc-fixes/`):

| Row | Image | Sol requests: stream + unary | Sol first sends: turns + unary | Sol re-sends | Per upstream, all ops (gated) | Generation only (recorded) | Opus, GLM | Requests / turn |
|---|---|---|---|---|---|---|---|---|
| rate 0.5 | `:qae2` | 49 + 95 | 41 + 95 | 8 | 1.059 | 1.195 | 1.000, 1.000 | 0.237 |
| rate 0.5 | `:e3misc` | 89 + 60 | 77 + 60 | 12 | **1.088** | 1.156 | 1.000, 1.000 | 0.364 |
| rate 0.25 | `:qae2` | 408 + 70 | 383 + 70 | 25 | 1.055 | 1.065 | 1.000, 1.000 | 1.211 |
| rate 0.25 | `:e3misc` | 409 + 75 | 375 + 75 | 34 | **1.076** | 1.091 | 1.000, 1.000 | 1.212 |

`make qa-fault ... FAULT_TESTS=tests/fault/test_f12_retry_storm.py`: `:e3misc` 2 passed, `:qae2` 2 passed (final test).
Reading: the run-to-run spread (Sol first sends 41-87 at `rate: 0.5`; breakers open at different moments) is larger
than R8's effect in one run. What R8 changes is the bound: the E2 floor allowed up to 10 re-sends per process window
(4 processes) regardless of traffic; at these volumes the re-sends were limited by demand, not by the budget. With R8
the re-sends are ≤ 10% of the key's first sends by construction (12 ≤ 13.7 at `rate: 0.5`). The deterministic
evidence is section 2. Cross-process sync lag (1 s) can let a burst overshoot by the re-sends granted elsewhere in
the last second; not seen here.

## 4. R4 metrics and the bounded Redis pool (IMPL-088)

Declared in `telemetry/registry.py` (and in `tests/unit/telemetry/test_registry_genai.py`'s exact list), observed in
`composition.py`:
- gauges every second (`telemetry.pool_gauges` task, every process with the real DB): `aghoo_db_pool_size{pool}`,
  `aghoo_db_pool_in_use{pool}`, `aghoo_redis_pool_size`, `aghoo_redis_pool_in_use` (size = the pool's maximum; Redis
  in-use from `ConnectionPool.get_connection_count()`, pub/sub holders included; Postgres in-use counted in `_DbPool`);
- histograms: `aghoo_db_pool_acquire_seconds{pool}`, `aghoo_db_command_seconds{pool}` (a unit of work, acquire to
  release), `aghoo_redis_pool_acquire_seconds`, `aghoo_redis_command_seconds{command}` (`MeteredRedis.execute_command`
  per command, `PIPELINE`/`MULTI` per pipeline; pub/sub commands are not counted). `pool` = `app`.
- The histograms observe a random 1 in 16 operations (`DATASTORE_SAMPLE_RATE`). Counts are 1/16 of the operations;
  sums, means and quantiles are unbiased. The shared seconds buckets start at 5 ms (otel.py, not this role's).

Redis pool: `MeteredPool(BlockingConnectionPool)` with `max_connections = NOEMA_REDIS_MAX_CONNECTIONS` (default 256,
8-10,000) and `timeout = 0.5 s`; a free or creatable connection is taken through `ConnectionPool.get_connection`
(no Condition, no timer); only an exhausted pool waits.

CPU per operation (`bench/bench_redis.py`, `bench_pools.py`, `bench_dbpool*.py`; dev Valkey, 20,000 sequential GETs or
5,000 two-command pipelines per round, median of 9 rounds, core 5 pinned):

| Variant | GET, µs CPU | Pipeline, µs CPU |
|---|---|---|
| Wave E2: `Redis.from_url` (unbounded `ConnectionPool`) | **85.6** | **128.2** |
| stock `BlockingConnectionPool`, no metrics | 99.9 | 144.3 |
| first version: stock blocking pool + every operation timed | 133.8 | 177.8 |
| shipped: fast-path blocking pool, metrics off | 91.7 | - |
| **shipped: fast-path blocking pool + 1/16 sampled metrics** | **95.1** (+9.5) | **136.4** (+8.2) |

Postgres wrapper (`_DbPool.acquire` over a no-op engine, the wrapper's own CPU, 50,000 units, median of 7): Wave E2
3.13 µs, shipped 4.03 µs (+0.9; every unit timed would be 16.65).

Per turn (`bench/per_turn.py` on `noema-e3misc-e2e`, 300 turns at concurrency 4, all 200; sampled counts x 16 over
the 4 api processes, net of an idle baseline of the same length): **14.5 Redis commands or pipelines** and **2.5
Postgres units of work** per turn. Added CPU per turn: 14.5 x 9.5 + 2.5 x 0.9 ≈ **0.14 ms**, about 0.23% of the
~60 ms of api CPU per turn (reports/perf/e-sprint-b.md). Timing every operation through the stock blocking pool would
have cost ~0.7 ms per turn.

Behavior evidence (`tests/integration/composition/test_datastore_metrics.py`, real composition on the seeded stack):
with 8 connections held, `ping()` fails after 0.45-2.0 s with `ConnectionError("No connection available")` (not
20 s, not `MaxConnectionsError`), and a ping waiting at the bound completes once a connection is released; after a
`start("jobs")`, SET/GET/PIPELINE, both acquire histograms and the DB command histogram have observations
(sampling forced to 1.0), `aghoo_redis_pool_size` = 64 (the env value), `aghoo_db_pool_size` = 20, Redis in-use ≥ 1
(the config watcher's subscription). `tests/unit/composition/test_datastore_settings.py`: pool class, bound,
timeout, keepalive; refused values 7 / 10001 / "many"; the sampler's rate (160,000 draws within ±0.003 of 1/16).

## 5. Estimator table override (IMPL-029c)

The admission fixer offered a `ProcessSettings` field (e.g. `NOEMA_TOKEN_ESTIMATOR_TABLE`) or a config field
(`models.<alias>.estimator`, a schema change in contracts-config). Chosen: the process variable (no schema change;
a refit is a deployment artifact). `ProcessSettings.token_estimator_table` (absolute path, else `StartupError`);
`composition._estimator_override` loads it with `load_table` at build; a missing, non-JSON or invalid file
(`chars_per_token` 0) refuses startup. Tests: shipped table Sol 100 chars = 11 + 22 tokens; refit 3 + 20; an alias
not in the refit counts chars ÷ 4 (`test_datastore_settings.py`, 10 tests with the pool ones).

## 6. Pub/sub heartbeat and M2

`noema.core.pubsub.heartbeat_messages(pubsub, clock)`: yields `message`/`pmessage`, PINGs after 5 s without traffic
(again every 5 s while quiet), raises `ConnectionError` after 15 s without any message or PONG; the time a listener
spends on a message is not silence (revocation.py's loop, generalized). Used by `RedisCoordinator._listen_once`
(`turn_cancel` + `session_released`; the old `_messages` helper is removed), `BreakerRegistry._subscriber`,
`DbCalibratorRegistry.listen`, `composition._config_watch`; each one's own except/backoff then resubscribes.
Tests: `tests/unit/core/test_pubsub_heartbeat.py` (3: pings at +5/+10 s and death at +15 s; a minute of answered
pings with a subscribe confirmation, yields only the published ones; slow handling is not silence),
`tests/unit/labeling/test_registry.py::test_a_silent_subscription_is_pinged_then_resubscribed` (2 PINGs, closed and
resubscribed after 15 s + the 1 s backoff); the coordinator integration tests (11) and `test_resilience_redis.py`
pass on real Valkey.

M2 (`tests/unit/turns/test_degradation_reporting.py`, one turn through `TurnService` with the real generation engine,
`MediaNegotiatorImpl`, `MediaBudgetPlannerImpl`, fake files; opus-deep at effort xhigh, Opus 5xx before any byte):
`preflight.completed` holds exactly {media_budget, active_content_removed, rejected} without `attempt_id`/`model`;
attempt 1 (Opus) {param_unsupported, not_ready, extraction_failed, size_limit} and attempt 2 (Sol) {param_unsupported,
not_ready, extraction_failed, model_capability}, each with its own `attempt_id` and Bedrock id; `route.selected` =
attempt 1's list; `turn.degradations` = preflight's; the union is every `DegradationReason` except `policy`, which no
Phase 1 code emits (grep: no producer). Mutation check: with `walk.py` not stamping `attempt_id` (overlay copy) the
test fails (`KeyError: 'attempt_id'`). Helper added to the fake: `FakeFileService.set_derivative`.

## 7. Rulings recorded (docs/IMPL-DECISIONS.md, docs/impl-spec/**)

| Ruling / item | Recorded in |
|---|---|
| **R8** (retry budget: re-sends the upstream may have received, 10% of first sends, no floor, unsent connect retry exempt once per call; F12 per failing upstream) | IMPL-016 amendment (the C2b floor bullet marked superseded) + index row; spec-02 §8 Wave E3 note; spec-10 F12 row; OWNER-CHECKPOINT "Wave E3 rulings" |
| **R9** (U+0000: stored model / turn text → U+FFFD; client-supplied structured fields such as session `metadata` → 400 `schema_violation`) | IMPL-022 amendment (resolves the E2 spec fixer's R7 discrepancy); spec-04 §10; OWNER-CHECKPOINT |
| Owner decision 2026-10-12 (sprint, then pods sized from the measured knee; live cap 60 → 87) | IMPL-080 amendment; spec-10 §6 heading and the §1 live row; OWNER-CHECKPOINT |
| E2 leftovers of the E2 spec report §3: SE13 waiver file `deploy/docker/.trivyignore.yaml` (incl. **spec-09 §9**); per-model `TOKEN_ESTIMATOR_V1` (defect 32); text-less stops as breaker failures (B1); F12 per upstream (H2) | spec-09 §9 (names the file, how `image-scan` and `sca_gate` read it, what it can waive); IMPL-029 (c) amendments + spec-11 §2; IMPL-078 amendment; IMPL-080 amendment |
| E3: estimator override, pub/sub heartbeat, bounded Redis pool, R4 metrics | IMPL-029 (c) amendment, spec-05 §5 (2 variables), spec-11 §2; IMPL-078 amendment; **IMPL-088** (new, index row, gap-coverage line 047…088); spec-02 §12 registry comment |

Not recorded (out of my paths): `docs/modules/integration.md` (integrator) should list the `telemetry.pool_gauges`
task and the two variables; the 08:L488 design patch (IMPL-087) is still pending in the design repo.

## 8. Tests changed outside the item list, and why

R8 removes the fresh-key grant, so tests that encoded the floor were updated minimally (each passes):
- `tests/contract_internal/test_health_registry.py`: two `RetryBudget` conformance tests note first sends before they
  expect a grant (the fake still passes; the real budget fails them without the notes).
- `tests/unit/adapters/test_complete.py`: `test_connect_retry_needs_the_retry_budget_unless_nothing_was_sent`
  (a RemoteProtocolError on a quiet key is not retried, a ConnectError is) replaces the floor-based test;
  `test_a_retry_after_a_possible_delivery_is_flagged` notes 10 first sends.
- `tests/integration/adapters/test_against_mockbedrock.py`: the mock's `connect_reset` is a TCP RST after the request
  write (possibly delivered): retried with 10 first sends, not retried on a quiet key (both asserted).
- `tests/integration/adapters/conformance_impls.py`: the mock adapter kit's budget gets `min_retries=1` so the shared
  connect-retry row still has one re-send on its quiet key; `test_resilience_redis.py`: 20 noted first sends.
- `tests/unit/composition/test_wiring.py`: `services.redis` is `MeteredRedis`; `tests/unit/telemetry/
  test_registry_genai.py`: the 8 new metrics in the exact registry list; `tests/unit/labeling/test_registry.py`:
  its fake PubSub answers `get_message` (the listener reads through the heartbeat helper).

## 9. Not done

- **Generation waste estimates per model** (item 4 of the admission fixer's hand-off): the only use is
  `src/noema/generation/run.py` `_usage_of` (`chars_per_token=CHARS_PER_TOKEN_V1`), and `generation/**` belongs to the
  perf-gc-objects agent of this sprint (who was editing `run.py` at the time). Patch for the generation owner:
  `chars_per_token=TOKEN_ESTIMATOR_V1.get(att.alias, (CHARS_PER_TOKEN_V1, 0))[0]` (import from `noema.config.models`;
  generation may not import `noema.ops`, so the `NOEMA_TOKEN_ESTIMATOR_TABLE` override would not reach it without a
  `TokenEstimator` method, a frozen-protocol change). Two expectations in `tests/unit/generation/
  test_usage_and_telemetry.py` change with it (Sol: 3 + 101 + 64 → 2 + 87 + 64 output tokens, the waste counter 168 →
  153). The output-side ratio differs from the input fit (Sol 4.97 vs 4.61 chars per token); a Phase 0 output fit
  would be better.
- The R8 exemption reads the handled exception (`sys.exception()`), because `_base._send` (adapters owner) calls
  the frozen `try_spend(account, model)`. An explicit `possibly_delivered` argument would be clearer.
- Histogram buckets below 5 ms for the datastore metrics need `otel.py` (frozen telemetry) to take per-metric bounds.
- C2's pool saturation and datastore latency are now measurable; the QA lead's re-gate measures them.

## 10. Checks

| Check | Command | Result |
|---|---|---|
| make check (final) | `NOEMA_AGENT_ID=e3-misc make -k check XDIST=4` | **all green**: ruff (916 files formatted, clean), mypy 327 src / 590 test files 0 issues, import-linter 27 kept / 0 broken, forbidden-scan clean, ownership ok, contracts-check (vendor identical, generated current), unit **3,442 passed**, conformance **757** (+8 skipped), integration **573** (+21 skipped), sdk TS 86 / Python 192 |
| make check (first, mid-sprint) | same | red only in other roles' in-flight files (lint in generation/turns/streaming tests, mypy in turns/service.py, test_heap.py) and in 3 of mine, fixed: the registry list test, the mock connect-reset test (R8), a task-group type |
| make e2e, my stack | `make e2e E2E_PROJECT=noema-e3misc-e2e E2E_PORT_BASE=33700 E2E_BUILD=0` (images `:e3misc`), then on rebuilt images with `E2E_PORT_BASE=23700` (twice; the last after the final edit) | **52 passed** (157.7 s), **52 passed** (157.4 s), **52 passed** (156.0 s, final tree) |
| F12, my fault stack | `make qa-fault FAULT_PROJECT=noema-e3misc-fault FAULT_PORT_BASE=33900 FAULT_TESTS=tests/fault/test_f12_retry_storm.py FAULT_IMAGE_TAG={e3misc,qae2}` | `:e3misc` 2 passed (Sol 1.088 / 1.076 per upstream); `:qae2` 2 passed (1.059 / 1.055) with the final test; section 3 |
| R8 unit tests before/after | `PYTHONPATH=<E2 overlay> pytest tests/unit/adapters/test_retry_budget.py` vs the tree | 9 failed, 7 passed vs 16 passed |
| targeted suites | `pytest tests/unit/adapters tests/contract_internal/test_health_registry.py tests/contract_internal/test_adapter.py tests/integration/adapters tests/security/test_sts.py` | 663 passed, 5 skipped (before the mock test fix); `test_against_mockbedrock.py` 25 passed after it |
| new tests | `pytest tests/unit/core/test_pubsub_heartbeat.py tests/unit/labeling/test_registry.py tests/unit/composition/test_datastore_settings.py tests/integration/composition/test_datastore_metrics.py tests/unit/turns/test_degradation_reporting.py` | 3 + 9 + 11 + 2 + 1 passed |
| teardown | `make e2e-down ...`, `make qa-fault-down ...` | no `noema-e3misc-*` container left |
