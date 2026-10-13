# Fault track (Wave E QA): F1-F12, the p95-TTFT breaker rule, slow clients

Role: fault (QA, Wave E), 2026-10-11. Gate ids: 06 §7 / §11 and spec-10 §3 (F1-F12, S1, C3) and §4 (the F rows).
Suite: `tests/fault/**` (22 tests), make target `qa-fault` (`make/qa-fault.mk`). No live Bedrock call, no AWS
resource touched: the wire mock, moto and toxiproxy only. Raw numbers: `reports/qa/fault/results.json` (run 1) and
`reports/qa/fault/results-run2.json` (run 2), plus `results-f1-standalone.json` (F1 in its final form, section 7);
this report quotes run 1 unless it says otherwise.

## 1. Setup (what was measured)

- **Stack**: `make qa-fault` = `make e2e-up E2E_PROJECT=noema-fault E2E_PORT_BASE=29300 E2E_DB_HOST=toxiproxy:15432
  E2E_BREAKER_TTFT=on` with `NOEMA_STACK_REDIS_URL=redis://toxiproxy:16379/0` and
  `NOEMA_STACK_MOTO_URL=http://toxiproxy:15000` (docs/modules/integration.md §5 "Fault runs"): two api replicas, jobs,
  media-worker, media-sandbox, the wire mock, and postgres / valkey / moto behind toxiproxy. Routing verified before
  the runs: the api containers' `NOEMA_REDIS_URL` and STS / Secrets Manager / S3 overrides point at toxiproxy, the
  `noema/db-dsn` secret at `toxiproxy:15432`, and disabling the `postgres` proxy turned `GET /v1/sessions` into 500
  "The database is temporarily unavailable." (healed on re-enable).
- **Breakers in production form**: `NOEMA_BREAKER_TTFT_RULE=on` (the e2e/UI/perf stacks run it `off`).
- **Revision tested: tag wave-c2b (b85f318).** Images `noema-{api,worker,media}:fault` = the c2b image ids
  (5c567ee08f4a, fd53aac3e0b7, b57d2b6873b4; a rebuild from the clean tree was a full cache hit). The compose tree is
  frozen too (`git archive wave-c2b deploy tools tests/fakes` into `/tmp/noema-fault-tree`, passed as
  `COMPOSE_FILE=`): perf sprint B edits `deploy/compose` concurrently, and its work-in-progress (`WEB_CONCURRENCY=2`
  per pod) recreated this stack's api containers on the c2b image, whose second worker then failed to start
  ("Address already in use" on the metrics port 9464). That is a WIP mismatch, not a c2b defect.
- **Traffic**: tenants t_acme (A) and t_beta (B), the `main` developer key of each; turn i goes to tenant `i % 2` and
  replica `(i // 2) % 2`; `Aghoo-Deadline-Ms: 10000` unless a row says otherwise; >= 200 turns per row (spec-10 §4;
  `NOEMA_FAULT_TURNS`). The platform config is the bootstrap config (dev price catalog) with the tenant rate limits
  lifted (spec-05 §1.2; one turn reserves 28 calls). A named profile is pinned with every pre-commit key
  (`generation: {profile, effort, max_output_tokens}`, `conditioning: "off"`, spec-10 §5), so the decided spec equals
  spec0 and a restart never mixes into a fault's numbers.
- **Mock timing (fixed, no distributions)**: router members answer at 200 ms (S1 precedes every first text), every
  stream starts its text at 600 ms and sends the 7 chunks of "The capital of France is Paris. It is also the largest
  city in the country." 50 ms apart; a labeler@1 rule answers sampled labeling jobs (they never fail against a
  generation model's breaker). Faults are selected by model, op and the turns' text tags (`f01-t_acme-...`).
- **Contract validation**: every response through the stack's `CheckedClient` (ContractValidator): error envelopes,
  JSON bodies, createTurn streams frame by frame as they arrive and whole at the end. The slow clients' raw bytes are
  de-chunked with h11 and every complete frame is validated with `check_sse_frame` (ids strictly increasing: coalesced
  frames leave id gaps).
- **S1 inside every row** (spec-10 §3): zero duplicated client-visible text (every `generation.delta` belongs to the
  committed attempt, a served turn renders the answer exactly once, any other turn a prefix of it), P-SSE-1
  (`tests.golden.compare.sse_invariants`), no HTTP 500 and no `error{internal_error}` (F11 excepted: there the ingress
  500 `internal_error`, retryable, is the specified `store: true` behavior), and the fallback success share.
- **Isolation between rows**: each row waits until 61 s after the previous one (the retry-budget windows of 60 s and
  the breaker windows of 30 s hold none of its outcomes), then every proxy is enabled with no toxic, the default
  scenario is loaded and every service circuit is closed on both replicas (a probe per tripped model: a router-default
  decision for the members, a `stream: false` turn pinned to sol-fast / opus-deep / glm-reason otherwise). F1 closes
  tenant A's Opus quota breaker (no gauge) in its `finally` (opus-deep turns until three in a row reach Opus).

## 2. Commands

```bash
# the suite (35.6 min for 22 tests; the stack stays up afterwards)
make qa-fault COMPOSE_FILE=/tmp/noema-fault-tree/deploy/compose/docker-compose.yml FAULT_IMAGE_TAG=fault \
    NOEMA_FAULT_TURNS=200 [FAULT_RESULTS=reports/qa/fault/results-run2.json] [FAULT_TESTS=tests/fault/test_f01_*.py]
make qa-fault-down        # stop the stack, delete its volumes
```

Without `NOEMA_FAULT=1` (set by the target) every test in `tests/fault` skips, so a plain `pytest` never drives
another agent's e2e stack. `make qa-fault` passes the api containers' IPs to pytest-socket (`--allow-hosts`) for the
slow clients, which connect to the containers directly (no docker-proxy buffering).

Runs: run 1 = 10 failed, 12 passed in 2,138 s; run 2 (same tree, same stack) = the same 10 failed and 12 passed in
2,136 s (`results-run2.json`). Both runs gave the same verdict per row; section 7 compares their numbers. After them,
F1 got its final form (an empty breaker window and a B-only warm-up before the throttled phase) and was run 4 more
times on its own (section 7).

## 3. Results per gate (run 1; `tests/fault/<test>`; every row also asserts S1 as in section 1)

| Gate | Test | Pass criterion | Measured | Verdict |
|---|---|---|---|---|
| F1 throttling | `test_f01_throttling.py` (200 turns, after 20 warm-up + 200 clean turns and a 31 s pause so the throttled phase starts with an empty breaker window, then 20 B-only warm-up turns; opus-deep; `throttling` on Opus for A only) | >= 99% of A's turns commit on the next member with no backoff sleep; B's TTFT p95 within 10% of a clean run | A: **75/100** committed on Sol: 40 turns reached Opus and were throttled, the retry budget granted **15** fallbacks and refused **25** (`error{all_routes_unavailable}`); the 60 later turns skipped Opus (A's quota breaker open) and went to Sol directly. No backoff: mock 429 sent -> Sol request arrives p95 59-68 ms. B: 100/100 on Opus, never throttled (the throttled requests carry A's two pod AKIDs only); TTFT p95 826.0 vs 756.8 ms clean (+9.1%) and 734.5 vs 746.4 ms (-1.6%) in the two runs of the final test (section 7) | **FAIL** (bug B1) |
| F2 AssumeRole failure | `test_f02_access.py::..._typed_503_before_any_model_call` (3 variants x 40 turns) | ingress 503 `tenant_access_misconfigured`, `retryable: false`, 0 mock requests; 100% classified | B moved to account 555566667777 (its cached STS session no longer matches): tenant A's ExternalId -> 20/20 503 cause `external_id_invalid`; missing ExternalId secret -> 20/20 `external_id_unavailable`; moto (STS + Secrets Manager) disabled in toxiproxy -> 20/20 `sts_unavailable`; all `retryable: false`, p95 21 ms, **0** mock requests carrying B's tags; A 60/60 completed meanwhile | **PASS** |
| F2 AccessDenied (generation) | `..._generation_access_denied_never_falls_back` (100 turns, opus-deep) | `error{tenant_access_misconfigured}`, no fallback | A: 50/50 `error{tenant_access_misconfigured, retryable: false}`, attempts = [Opus] only, **0** Sol requests; B 50/50 completed on Opus; Opus service breaker closed afterwards on both pods (tenant access is not provider health) | **PASS** |
| F2 AccessDenied (decision member) | `..._decision_member_access_denied_degrades_the_decision` (100 turns) | member degraded (IMPL-057), turn completes | 100/100 completed; 500/500 Haiku votes `error` `tenant_access_misconfigured`; decisions `completed` 44 / `quorum` 56; Haiku breaker closed | **PASS** |
| F3 5xx burst | `test_f03_burst.py` (550 turns at 10/s; `http_5xx{503}`, `window_ms [0, 30000]`, `rate 1.0` on Sol) | breaker open inside the 30 s window once >= 20 requests saw > 50% errors; half-open 15 s later; closed after the window; time-to-open / -recover reported | **time-to-open 4.08 s** (after 38 failed requests, i.e. >= 20 per pod; both pods open within one 250 ms poll via breaker_events); 254/255 turns sent while open refused without a model call; a failed half-open probe at ~19 s re-opened it for 15 s; half-open at 34.08 s, **closed at 34.95 s = 4.95 s after the window**; 205/205 turns after recovery completed; every burst turn typed (`all_routes_unavailable`, sol-fast has no other member) | **PASS** |
| F3b p95-TTFT rule | `test_f03_ttft_breaker.py` (412 turns at 2.5/s; rule armed) | the TTFT rule (window p95 > 3x baseline, >= 20 samples, armed after 3 windows) opens the breaker on latency alone | no trip during the 80 s baseline (TTFT p95 620.8 ms); Sol's first text 600 -> 2,400 ms (every call still succeeds, TTFT p95 2,421 ms): **opened 4.49 s after the slowdown** on both pods; 43/100 slow-phase turns refused without a call; a half-open probe success closes it while latency is still high (closed 103.4 s), the refilled window re-opens it (122.3 s); after the restore to 600 ms: **closed 15.04 s later** | **PASS** |
| F4 hang (GLM, fallback) | `test_f04_hang.py::test_f04_glm_hang_...` (100 turns, glm-reason, deadline 30 s) | fallback to Sol at the 10 s first-event timeout; every e2e <= deadline + 1 s | first attempt failed at 10.31 s p50 / 10.84 s p95; e2e max 11.9 s (<= 31 s); 50 turns skipped GLM once its breaker opened; fallback **40/50** (10 refused by the retry budget, `all_routes_unavailable`) | **FAIL** (S1 fallback; bug B1) |
| F4 hang (Sol, no member left) | `test_f04_sol_hang_...` (100 turns, sol-fast, deadline 10 s) | `error{all_routes_unavailable}` at 5 s; e2e <= deadline + 1 s | 100/100 `all_routes_unavailable`; error at 5.13 s p50 / 5.50 s p95 (first-event timeout 5 s); e2e max 5.86 s; 35 turns refused at once by the open Sol breaker | **PASS** |
| F5 stall pre-commit | `test_f05_stall.py::test_f05_pre_commit_...` (100 turns, glm-reason: reasoning, then `stall_after{n: 0}`; deadline 45 s) | fallback | first-answer timeout at 30 s, e2e p50 30.07 s / max 31.95 s; fallback **40/53** (13 refused) | **FAIL** (S1 fallback; bug B1) |
| F5 stall post-commit | `test_f05_post_commit_...` (100 turns, sol-fast, `stall_after{n: 3}`) | `error{upstream_stalled, retryable: true}`, partial text kept and stored | 100/100 `upstream_stalled`, retryable, at 31.8 s p50 (idle timer 30 s after the 3rd chunk); rendered text = the 3 chunks 100/100; stored text = the 3 chunks 20/20 checked (getTurn on the other replica), attempt `committed` | **PASS** |
| F6 stream exception pre-commit | `test_f06_stream_exception.py::..._pre_commit_...` (100 turns, opus-deep, `modelStreamErrorException` after 0 chunks) | fallback; 100% classified | 53 first attempts failed, 47 skipped Opus (breaker open); fallback **24/53** (29 refused, typed `all_routes_unavailable`) | **FAIL** (S1 fallback; bug B1) |
| F6 stream exception post-commit | `..._post_commit_...` (100 turns, sol-fast, after 5 chunks; 5 exception types) | `error{all_routes_unavailable, retryable: true}`; the committed chunks stay the only text | every committed turn (11-13 per type) ended `all_routes_unavailable`, retryable, with exactly the 5 chunks rendered, for modelStreamError, internalServer, serviceUnavailable, throttling and validationException; the other turns were refused by the Sol breaker these failures opened (typed) | **PASS** |
| F7 reasoning-only / empty text | `test_f07_reasoning_only.py` (100 turns, glm-reason; `reasoning_only` and `empty_text` on GLM) | nothing committed from the failed attempt; fallback to Sol; 0 `generation.completed` with empty text | **0** completed with empty text, **0** commits from GLM (no empty answer delivered); fallback **40/100** (60 refused): text-less stops are `ok` for the breaker, so GLM's never opens and every turn needs a fallback | **FAIL** (S1 fallback; bug B1) |
| F8 malformed stream (attempt) | `test_f08_malformed.py::test_f08_a_...` (100 turns, opus-deep; one chunk, then `malformed{bad_crc}` or `truncated_json`, held pre-commit) | attempt -> fallback; 0 HTTP 500, 0 `internal_error` | **0** HTTP 500, **0** `internal_error`; the held chunk of the failed attempt never rendered; fallback **27/40** (13 refused) | **FAIL** (S1 fallback; bug B1) |
| F8 invalid decision output | `test_f08_invalid_decision_output_...` (100 turns) | member `error` with its parse code; turn served | 100/100 completed; Luna `missing_options` / `sum_out_of_range` / `parse_error`, Haiku `parse_error`, DeepSeek (Mantle truncated or broken JSON) `upstream_error` (plus `late` for its backup); never `ok` | **PASS** |
| F9 all members down | `test_f09_members_down.py` (200 turns; `http_5xx{503}` on every member) | 100% of turns served; reason "decision unavailable: all_members_failed" | **200/200** completed on sol-fast (TTFT p95 790 ms); all 1,000 answers `unavailable`; routing reason `all_members_failed` 181, **`no_quorum` 19**; 1.14 member requests per turn (member breakers open) | **PASS** (06 §7); reason string 181/200 (bug B5, minor) |
| F10 Redis down | `test_f10_redis_down.py[down]` (225 turns at 5/s; valkey proxy disabled 25 s) | >= 99.9% of the turns complete; local GCRA buckets; L2 bypass; same-pod SSE unaffected; cross-pod cancel 500; cross-pod attach waits then 410 (IMPL-021) | **125/125** turns sent during the outage completed (e2e p50 921 ms), 40/40 after; `aghoo_admission_degraded` 1 on both pods; L2 `aghoo_cache_misses_total{reason="error"}` +255 (`bypass` +0); same-pod cancel 202, cross-pod cancel 500; **cross-pod idempotent duplicate: 500 `internal_error` "The original request can't be attached; retry." after 6 ms** while the original was still streaming | **PARTIAL** (attach item FAIL: bug B4) |
| F10 Redis hung | `test_f10_redis_down.py[hung]` (same; toxiproxy `timeout` toxic, connections open, no byte passes) | as F10 | 125/125 completed during the outage, 40/40 after; admission degraded on both pods; L2 errors +252; same-pod cancel 202, cross-pod cancel 500; every turn ~550 ms slower while Redis hangs (e2e p50 1,468 ms vs 921 ms); attach: 500 `internal_error` after 5 ms | **PARTIAL** (attach item FAIL: bug B4) |
| F11 Postgres down | `test_f11_postgres_down.py` (380 turns at 4/s, half `store: true`, half `store: false`; 10 slow in-flight turns; postgres proxy disabled 60 s) | `store: true` -> 500 `internal_error` (retryable) at ingress; `store: false` unaffected; in-flight finalize -> outbox; after recovery every finalized turn and usage line exactly once | `store: true`: 120/120 500 `internal_error` retryable in 4.2 ms p50; in-flight: 10/10 streamed to the end (finished 4.8 s into the outage), finalize queued in the outbox; after recovery **80/80** checked turns stored exactly once with exactly one `generation` usage line each (database query); `store: false`: 100/100 completed in the first 50 s, then **19/20 failed** with 500 "The database is temporarily unavailable." in the +50..60 s bucket (the API key cache entry expired: 60 s TTL, no stale use) | **FAIL** (bug B3) |
| F12 retry storm | `test_f12_retry_storm.py[0.5]` (750 turns at 12.5/s for 60 s; sol-fast / opus-deep / glm-reason; `http_5xx{503}` `rate 0.5` on Sol, Opus, GLM) | generation requests at the mock / turns <= 1.1 over 60 s | **0.372** requests per turn sent (279 / 750; 1.094 per turn that made an attempt): the three service breakers opened (> 50% errors) and 495 turns were refused without a call; 140/750 turns served. Load label: spec-10 asks 5x the C1 load (200 streams per pod); this stack's processes sustain ~19 turns/s each (reports/perf/c2b-perf-smoke.md), so the row ran at 12.5 turns/s | **PASS** (amplification) |
| F12 retry storm, breakers closed | `test_f12_retry_storm.py[0.25]` (informational: `rate 0.25`) | as F12 | **1.045** (784 / 750): the retry budget alone bounds it; 178 first attempts failed, **30** fallbacks granted (25 served), 148 turns ended `all_routes_unavailable` | **PASS** (amplification); fallback availability: bug B1 |
| S1 zero duplicated text | asserted in every row (about 5,100 turns per run) | every client's rendered text = the committed attempt's text once | **0** problems in every row: no delta of an uncommitted attempt reached a client (failed / held attempts in F1, F4-F8, F12), every served turn rendered the answer exactly once, every errored turn a prefix | **PASS** |
| S1 fallback success | asserted in every row with a fallback member | >= 99% of turns whose first attempt failed pre-commit are served | F1 37.5% (15/40), F4 80% (40/50), F5 75% (40/53), F6 45% (24/53), F7 40% (40/100), F8 67.5% (27/40); every fallback that the budget **granted** was served (F1-F8: 186/186; F12: 40/47, the fallbacks there hit the 50% / 25% faults themselves) | **FAIL** (bug B1) |
| C3 slow clients (fault-suite scale) | `test_c3_slow_clients.py` (4 slow clients at 10% of a 126 KB/s stream among 8 normal ones; 90 s streams of 2,700 4,000-char chunks) | coalescing, then `client_too_slow`, no effect on the other streams (normal TTFT p95 within 10% of a baseline) | coalescing: yes (every slow client was receiving a ~625 KB frame, vs 4.2 KB per chunk frame); normal clients: 8/8 completed the whole text, TTFT p95 709.9 ms vs 884.5 ms baseline; **client_too_slow detected (api log "sse client too slow; stream closed" ~46 s into the streams, run 2) but never applied: 4/4 slow turns stored `completed` at 90.6 s**, the stream kept being generated and the slow connections stayed open | **FAIL** (bug B2) |
| S2 availability 99.9% | — | prod | not measured here (prod gate) | NOT-RUN (prod) |


## 4. Bugs found (module code; reproducers on the fault stack)

**B1 (major; generation + adapters): the retry budget refuses most pre-commit fallbacks, so S1 "fallback success
>= 99%" fails in every fallback row (F1, F4, F5, F6, F7, F8).** `StreamingProfileRun._start`
(`src/noema/generation/walk.py`) spends `RetryBudget.try_spend(tenant account, <fallback member's model id>)` for every
failure-driven attempt; `SlidingRetryBudget` (`src/noema/adapters/resilience/retry_budget.py`) grants
`max(min_retries = 10, 0.1 x first attempts)` per 60 s per (account, model), summed over the pods. When the failing
route's fallback member (Sol) carries few first attempts of that account, the floor (10-15 grants a minute with two
pods syncing every second) is all there is, while the failing model's breaker needs `min_requests` 20 per pod before
later turns skip it for free; for text-less stops (`reasoning_only`, `empty_text`) the breaker never opens at all
(`_record_health` records them `ok`), so every turn needs a fallback. Measured: 25 of tenant A's 100 throttled-row
turns, 10/50 (F4), 13/53 (F5), 29/53 (F6), 60/100 (F7), 13/40 (F8) ended `error{all_routes_unavailable}` without the
fallback being tried; every granted fallback was served (186/186). 06 §7 F1 asks "immediate fallback to another model"
with >= 99% success, and F12 caps amplification at 1.1x: the budget satisfies F12 (1.045) by refusing the fallbacks
F1-F8 need. Repro: `make qa-fault COMPOSE_FILE=... FAULT_IMAGE_TAG=fault FAULT_TESTS=tests/fault/test_f07_reasoning_only.py`
(numbers `F7.*.fallback`: `fallback_started` 20 of `first_attempt_failed` 50 per kind). Options for the fixers: do not
charge a fallback to a *different* model (or a throttle fallback) to the budget, key it on the failed model, or raise
the floor to at least the breakers' `min_requests` x pods; the IMPL-016 amendment (request item 8) should rule.

**B2 (major; api, SSE): `client_too_slow` is detected but never applied while the slow client stays connected.**
`SseResponse.__call__` (`src/noema/api/sse_frame.py`, c2b) awaits the final `send({"type": "http.response.body",
"body": b"", "more_body": False})` after the writer returned `client_too_slow`, and only then calls
`on_close(outcome)`, which is what cancels the turn (`runner.request_cancel("failed")`, routes/turns.py). uvicorn's
`send` first waits for the transport to drain (flow control), i.e. for the slow client to read its whole backlog
(here a ~625 KB coalesced frame at ~12 KB/s), so the turn keeps generating (Bedrock cost), the connection stays open
and the turn ends `completed` when the model finishes. Measured (run 2): detection at ~46 s into a 90 s stream on both
pods, 4/4 slow turns stored `completed` at 90.6 s. A single-client probe showed the same: detection 47.8 s after the
start, the cancel applied only when the client disconnected at 80 s (stored `failed` then). Repro: `make qa-fault ...
FAULT_TESTS=tests/fault/test_c3_slow_clients.py` (`C3-slow-clients.slow_stored`). Fix: run `on_close` (the cancel)
before any further send on `client_too_slow`, and bound or skip the closing body send (close the transport).

**B3 (major; identity): during a Postgres outage every request fails once an API key's 60 s cache entry expires, so
`store: false` turns are not "unaffected" (06 §7, spec-10 F11).** `ApiKeyAuthenticator._lookup`
(`src/noema/identity/authn.py`) reloads an expired `KeyCache` entry from Postgres and answers 500 `internal_error`
"The database is temporarily unavailable." when the read fails; the expired row is never used as a fallback.
Measured: `store: false` 100/100 for the first 50 s of a 60 s outage, then 19/20 failed (`F11.store_false_by_10s`),
for the same two keys that had been in use all along. Repro: `make qa-fault ... FAULT_TESTS=tests/fault/test_f11_postgres_down.py`.
Fix: keep serving the last good row while the store is unavailable (bounded, e.g. revocations still apply through
`keys_revoked` while Redis is up); spec-10 F11 and the revocation bound need a joint ruling.

**B4 (minor; api, idempotency): with Redis down a cross-pod idempotent duplicate answers 500 at once instead of
waiting for the original and answering 410 (IMPL-021: "a cross-pod attach waits for the original to finish
(IMPL-077), then returns 410").** `IdempotencyHook._attach` (`src/noema/api/middleware/idempotency.py`) raises
`NoemaError(INTERNAL_ERROR, "The original request can't be attached; retry.")` when `attach_remote` returns None.
Measured: 500 after 6 ms (`down`) / 5 ms (`hung`) while the original was still streaming on api-1. Repro:
`make qa-fault ... FAULT_TESTS=tests/fault/test_f10_redis_down.py` (`F10-*.cross_pod_attach`). The client does get a
retryable error, hence minor; a retry during the outage loops on 500.

**B5 (minor; decision): with every member failing, 19/200 turns report `routing.reason` "decision unavailable:
no_quorum" instead of `all_members_failed` (spec-10 F9 / G09).** `classify` (`src/noema/decision/answer.py`) counts a
member whose call has not finished when the snapshot is taken as `late` (`status_of`), so a snapshot in which every
finished member is `error` and one is still pending falls to rule 7 (`no_quorum`) rather than rule 4 (which member
was pending was not recorded per turn). The turns are served (06 §7 holds); only the reason differs. Repro:
`make qa-fault ... FAULT_TESTS=tests/fault/test_f09_members_down.py` (`F9.routing_reason`).

## 5. Observations (not filed as bugs)

- **Hung Redis costs every turn ~550 ms** (F10 `hung`: e2e p50 1,468 ms vs 921 ms with Redis refused): some
  per-turn Redis call waits for its timeout before falling back. Availability holds (125/125).
- **TTFT breaker flaps under a sustained slowdown** (F3b): a half-open probe that succeeds closes the breaker even
  though the model is still 4x slower, the window refills with slow samples and it re-opens ~19 s later
  (open 85.3 s -> closed 103.4 s -> open 122.3 s). As designed (`_observe`: a half-open success closes), but each
  cycle lets ~20 slow turns per pod through.
- **F3 recovery depends on the phase of the open interval**: a probe that fails just before the window ends re-opens
  the breaker for another `open_s` (15 s), so time-to-recover after a 30 s burst ranges up to ~15 s (4.95 s here).
- **The slow-client backlog is large before anything reacts** (C3): ~1 MiB coalesced into one frame plus the kernel
  and transport buffers per slow connection; the detection came ~46 s into the stream (the 30 s timer starts only once
  a send blocks or the queue stays full, after those buffers filled).
- **F2 alert counter**: `aghoo_tenant_access_failures_total{tenant="t_beta"}` did not grow during the measured window
  (0 per variant): the failure is counted once per refresh, and the measured turns were answered from the broker's
  10 s negative cache filled by the readiness probe just before. The alert path itself was not re-verified here.
- **F12 at `rate 0.5` is breaker-dominated**: 66% of the turns were refused without a call; the amplification gate
  passes trivially (0.372). The `rate 0.25` row shows the retry budget alone (1.045).
- **Mid-stream `validationException`** (F6): mapped to `all_routes_unavailable`, retryable, like the
  provider-side types; IMPL-076 maps fatal and invalid-request pre-commit failures to `internal_error`, which F8
  never produced (0 `internal_error` in every row).

## 6. Not run, or run reduced (with the reason)

| Item | Status | Reason |
|---|---|---|
| STS `AccessDenied` from STS itself (F2) | NOT-RUN here | moto's AssumeRole never denies (a non-existent role and a wrong ExternalId both succeed); the classification is covered by SEC-STS-05/06/07/09 with `trust_enforcer` (identity tests), not re-run by this track |
| F12 at 5x the C1 load | reduced | the stack's api processes sustain ~19 turns/s each; the row ran at 12.5 turns/s for 60 s (750 turns), labeled |
| C3 at full scale (20 slow among 180, `client_too_slow` at 30 +/- 1 s, queue high-water <= 256 events / 1 MiB) | reduced | the load track's gate (`tests/load`, QA lead); here 4 slow among 8 normal; the high-water marks are not exported as metrics |
| 06 §7 "toxiproxy in front of live-staging" | NOT-RUN | live Bedrock calls belong to the live-smoke agent (60-call budget) |
| F11 "no data loss" beyond turns and generation usage lines | partial | decision rows, `waste` lines and outbox dead letters were not counted |
| G12 R4 / abandon watchdog, Last-Event-ID resume | NOT-RUN | not fault rows (spec-10 §4); resume is 501 in Phase 1 (IMPL-036) |
| S2 availability 99.9% monthly | NOT-RUN | production gate |

## 7. Run-to-run stability (run 1 / run 2 of the whole suite)

| Row | Run 1 | Run 2 |
|---|---|---|
| F1 (suite form: no pause between the clean and the throttled phase) | A 75/100 on Sol, fallbacks 15/40 granted; B TTFT p95 743.5 vs 736.4 ms | A **17/100** on Sol, fallbacks 17/100: A's quota breaker never opened, because the clean phase's `ok` outcomes were still in its 30 s window (timing-dependent); B 722.7 vs 785.4 ms |
| F1 (final form, 4 standalone runs) | fallbacks 15/41, 13/40, 15/40, 15/40; A on Sol 74, 73, 75, 75 of 100; B TTFT p95 vs clean +22.8%, +22.2% (first two runs, before the B-only warm-up was added: the measured phase started cold after the pause), then **+9.1%, -1.6%** | |
| F2 (3 variants, generation, member) | 60/60 typed 503, 0 mock requests; 50/50 typed error, 0 Sol requests; 100/100 completed | identical |
| F3 open / recover | 4.08 s / 4.95 s after the window; 254/255 refused while open | 4.03 s / 4.98 s; 256/257 |
| F3b open / close | 4.49 s after the slowdown / 15.04 s after the restore | 4.45 s / 14.80 s |
| F4 GLM / Sol | fallbacks 40/50, e2e max 11.9 s / error p95 5.50 s, e2e max 5.86 s | 40/50, 11.6 s / 5.71 s, 6.09 s |
| F5 pre / post | fallbacks 40/53 / 100/100 `upstream_stalled` at 31.8 s p50 | 40/54 / 100/100 at 32.0 s |
| F6 pre / F7 / F8 attempt | fallbacks 24/53 / 40/100 / 27/40 | 20/55 / 40/100 / 26/40 |
| F9 | 200/200 served; `no_quorum` 19 | 200/200; `no_quorum` 17 |
| F10 down / hung | 125/125 and 125/125 during the outage; attach 500 | identical; hung e2e p50 1,467 ms |
| F11 | `store: false` 19/20 failed at +50 s; 80/80 turns and lines once | identical |
| F12 rate 0.5 / 0.25 | amplification 0.372 (1.094 per attempted turn) / 1.045 | 0.377 (1.084) / 1.043 |
| C3 | 4/4 slow turns stored `completed`; normal TTFT p95 709.9 vs 884.5 ms | 4/4 `completed`; 722.0 vs 929.7 ms |

## 8. Deliverables and checks

- Suite: `tests/fault/conftest.py` (fixtures, the NOEMA_FAULT gate, results file), `kit.py` (scenarios, driver,
  S1 checks, statistics), `stackctl.py` (toxiproxy, config versions, breaker monitor and heal, metrics, SQL),
  `slowclient.py`, `test_c3_slow_clients.py`, `test_f01_throttling.py` ... `test_f12_retry_storm.py` (22 tests).
- Make: `make/qa-fault.mk` (`qa-fault`, `qa-fault-up`, `qa-fault-down`).
- `ruff check` / `ruff format --check` / `mypy --strict` clean on `tests/fault`; `make typecheck-tests` green (541
  files). `make lint` currently fails only on `tests/unit/streaming/test_event_log.py:133` (PERF401; perf sprint B's
  work in progress, not this track). `make ownership` reports `make/qa-fault.mk` (like `make/qa-contract.mk` and
  `make/qa-security.mk`) UNOWNED until OWNERS.yaml gets the rule requested in `.orchestration/requests/qa.md` item 1
  (`{tests/qa/**,make/qa-*.mk}` -> `[qa]`); `tests/fault/**` and `reports/**` are owned by qa.
- `tests/fault` is not part of `make check` (it needs the fault stack); without `NOEMA_FAULT=1` it skips.
- The fault stack (`noema-fault`, ports 29300+) was torn down after the runs (`make qa-fault-down`, volumes
  deleted); `noema-dev` was left up. The image tags `noema-{api,worker,media}:fault` and the frozen compose tree in
  `/tmp/noema-fault-tree` remain for a re-run of this revision.
